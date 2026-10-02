"""providers.py — LLM API clients and the provider cascade.

Each call_* function sends the OCR text to one provider and returns (content, usage).
get_raw_llm_response() tries providers in priority order behind circuit breakers.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request

from src.config import (
    settings,
    has_cloudflare_key,
    has_gemini_key,
    has_groq_key,
    has_openrouter_key,
)
from src.extraction.prompts import SYSTEM_PROMPT
from src.resilience import CircuitBreaker, with_retries

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger(__name__)

# Upper bound on any single provider HTTP request, so a hung connection cannot stall a batch.
LLM_HTTP_TIMEOUT_SECONDS = 45

_gemini_client = None


def _get_gemini_client():
    """Build the Gemini client on first use rather than at import time."""
    global _gemini_client
    if _gemini_client is None and genai and has_gemini_key():
        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


def _log_llm_request(provider: str, model: str, filename: str, prompt: str) -> None:
    """Log request metadata only — never the document text, which carries supplier/bank data."""
    logger.info(
        "LLM request: provider=%s model=%s file=%s chars=%d est_tokens=~%d",
        provider, model, filename or "DOCUMENT", len(prompt), len(prompt) // 4,
    )


class QuotaExhaustedError(RuntimeError):
    """Raised when LLM API rate limit or quota is exhausted (HTTP 429 RESOURCE_EXHAUSTED)."""
    pass


class ProviderConfigError(ValueError):
    """Raised when a provider's API key or client is missing or invalid."""
    pass


# Errors a retry cannot fix: skip the backoff and let the cascade try the next provider.
NON_RETRYABLE_ERRORS = (QuotaExhaustedError, ProviderConfigError)


@with_retries(max_retries=2, base_delay=2.0, no_retry_on=NON_RETRYABLE_ERRORS)
def call_groq_api(ocr_text: str, filename: str = "") -> tuple[str, dict]:
    """Call Groq API (OpenAI-compatible Chat Completions) via stdlib urllib.request."""
    if not has_groq_key():
        raise ProviderConfigError("settings.groq_api_key is missing or invalid.")

    full_prompt_input = f"{SYSTEM_PROMPT}\n\nDOCUMENT TEXT:\n{ocr_text}"

    _log_llm_request("Groq", settings.groq_model, filename, full_prompt_input)

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"DOCUMENT TEXT:\n{ocr_text}"}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_HTTP_TIMEOUT_SECONDS) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            content = resp_data["choices"][0]["message"]["content"]
            return content.strip(), resp_data.get("usage", {})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        if e.code == 429 or "rate_limit_exceeded" in err_body.lower() or "quota" in err_body.lower():
            raise QuotaExhaustedError(f"Groq API Quota Exhausted ({settings.groq_model}): HTTP {e.code} - {err_body}") from e
        if e.code == 400 and ("json_validate_failed" in err_body.lower() or "validate json" in err_body.lower()):
            # Retry without response_format constraint
            payload_retry = dict(payload)
            payload_retry.pop("response_format", None)
            req_retry = urllib.request.Request(
                url,
                data=json.dumps(payload_retry).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req_retry, timeout=LLM_HTTP_TIMEOUT_SECONDS) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    content = resp_data["choices"][0]["message"]["content"].strip()
                    # Clean markdown code block or extract JSON substring
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0].strip()
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0].strip()
                    start_idx = content.find("{")
                    end_idx = content.rfind("}")
                    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                        content = content[start_idx:end_idx+1]
                    return content, resp_data.get("usage", {})
            except Exception as retry_err:
                raise RuntimeError(f"Groq API Call Failed: HTTP 400 - {err_body}") from retry_err

        raise RuntimeError(f"Groq API Call Failed: HTTP {e.code} - {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"Groq API Error: {e}") from e


@with_retries(max_retries=2, base_delay=2.0, no_retry_on=NON_RETRYABLE_ERRORS)
def call_openrouter_api(ocr_text: str, filename: str = "") -> tuple[str, dict]:
    """Call OpenRouter API (OpenAI-compatible Chat Completions) via stdlib urllib.request."""
    if not has_openrouter_key():
        raise ProviderConfigError("OPEN_ROUTER_API key is missing or invalid.")

    full_prompt_input = f"{SYSTEM_PROMPT}\n\nDOCUMENT TEXT:\n{ocr_text}"

    _log_llm_request("OpenRouter", settings.open_router_model, filename, full_prompt_input)

    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": settings.open_router_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"DOCUMENT TEXT:\n{ocr_text}"}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 4096
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.open_router_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_HTTP_TIMEOUT_SECONDS) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            content = resp_data["choices"][0]["message"]["content"].strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                content = content[start_idx:end_idx+1]
            return content, resp_data.get("usage", {})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        if e.code == 429 or "rate_limit" in err_body.lower() or "quota" in err_body.lower():
            raise QuotaExhaustedError(f"OpenRouter API Quota Exhausted ({settings.open_router_model}): HTTP {e.code} - {err_body}") from e
        raise RuntimeError(f"OpenRouter API Call Failed: HTTP {e.code} - {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"OpenRouter API Error: {e}") from e


@with_retries(max_retries=2, base_delay=2.0, no_retry_on=NON_RETRYABLE_ERRORS)
def get_raw_gemini_response(ocr_text: str, filename: str = "") -> tuple[str, dict]:
    """Call Gemini API directly and return the raw unparsed JSON string response."""
    client = _get_gemini_client()
    if client is None:
        raise ProviderConfigError("Gemini API key is missing or invalid.")

    full_prompt_input = f"{SYSTEM_PROMPT}\n\nDOCUMENT TEXT:\n{ocr_text}"

    _log_llm_request("Gemini", settings.gemini_model, filename, full_prompt_input)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[SYSTEM_PROMPT, f"DOCUMENT TEXT:\n{ocr_text}"],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            )
        )
        return response.text.strip(), {}
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            raise QuotaExhaustedError(f"Gemini API Quota Exhausted ({settings.gemini_model}): {e}") from e
        raise e


@with_retries(max_retries=2, base_delay=2.0, no_retry_on=NON_RETRYABLE_ERRORS)
def call_cloudflare_workers_ai_api(ocr_text: str, filename: str = "") -> tuple[str, dict]:
    """Call Cloudflare Workers AI API via stdlib urllib.request."""
    if not has_cloudflare_key():
        raise ProviderConfigError("CLOUDFLARE_WORKERS_AI key is missing or invalid.")

    full_prompt_input = f"{SYSTEM_PROMPT}\n\nDOCUMENT TEXT:\n{ocr_text}"

    _log_llm_request("Cloudflare", settings.cloudflare_model, filename, full_prompt_input)

    if settings.cloudflare_account_id:
        url = f"https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}/ai/v1/chat/completions"
        payload = {
            "model": settings.cloudflare_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"DOCUMENT TEXT:\n{ocr_text}"}
            ],
            "temperature": 0.1
        }
    else:
        # Fallback to general AI gateway / direct worker format if account_id is not specified
        url = f"https://api.cloudflare.com/client/v4/ai/v1/chat/completions"
        payload = {
            "model": settings.cloudflare_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"DOCUMENT TEXT:\n{ocr_text}"}
            ],
            "temperature": 0.1
        }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.cloudflare_workers_ai_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_HTTP_TIMEOUT_SECONDS) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            if "choices" in resp_data and resp_data["choices"]:
                content = resp_data["choices"][0]["message"]["content"].strip()
            elif "result" in resp_data and isinstance(resp_data["result"], dict) and "response" in resp_data["result"]:
                content = resp_data["result"]["response"].strip()
            else:
                content = str(resp_data)

            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                content = content[start_idx:end_idx+1]
            return content, resp_data.get("usage", {})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        if e.code == 429 or "rate_limit" in err_body.lower() or "quota" in err_body.lower():
            raise QuotaExhaustedError(f"Cloudflare Workers AI Quota Exhausted ({settings.cloudflare_model}): HTTP {e.code} - {err_body}") from e
        raise RuntimeError(f"Cloudflare Workers AI Call Failed: HTTP {e.code} - {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"Cloudflare Workers AI Error: {e}") from e


def provider_model(provider: str) -> str:
    """The configured model name for a provider in the cascade."""
    return {
        "OpenRouter": settings.open_router_model,
        "Cloudflare": settings.cloudflare_model,
        "Groq": settings.groq_model,
        "Gemini": settings.gemini_model,
    }.get(provider, "")


# Default cascade order, cheapest model first. Paid list prices per 1M input/output tokens
# for the default models in src/config.py, checked 2026-10-02:
#   OpenRouter  meta-llama/llama-3.2-3b-instruct   $0.05 / $0.33
#   Cloudflare  @cf/meta/llama-3.1-8b-instruct     $0.282 / $0.827
#   Groq        llama-3.3-70b-versatile            $0.59 / $0.79
#   Gemini      gemini-3.5-flash                   $0.75 / $4.50
DEFAULT_PROVIDER_ORDER = ("OpenRouter", "Cloudflare", "Groq", "Gemini")

openrouter_breaker = CircuitBreaker("OpenRouter", failure_threshold=2, cooldown_seconds=60)
groq_breaker = CircuitBreaker("Groq", failure_threshold=2, cooldown_seconds=60)
cloudflare_breaker = CircuitBreaker("Cloudflare", failure_threshold=2, cooldown_seconds=60)
gemini_breaker = CircuitBreaker("Gemini", failure_threshold=2, cooldown_seconds=60)

def get_raw_llm_response(ocr_text: str, filename: str = "") -> tuple[str, dict, str]:
    """Route LLM extraction call dynamically prioritizing settings.primary_provider, then falling back to others.

    Returns (content, usage, provider_name) from the first provider that succeeds.
    """
    
    # Define provider execution blocks
    def run_openrouter():
        if has_openrouter_key() and openrouter_breaker.can_execute():
            try:
                res = call_openrouter_api(ocr_text, filename=filename)
                openrouter_breaker.record_success()
                return res
            except Exception as e:
                openrouter_breaker.record_failure()
                print(f"OpenRouter API failed ({e}), trying fallback providers...", file=sys.stderr)
        return None

    def run_groq():
        if has_groq_key() and groq_breaker.can_execute():
            try:
                res = call_groq_api(ocr_text, filename=filename)
                groq_breaker.record_success()
                return res
            except Exception as e:
                groq_breaker.record_failure()
                print(f"Groq API failed ({e}), trying fallback providers...", file=sys.stderr)
        return None

    def run_cloudflare():
        if has_cloudflare_key() and cloudflare_breaker.can_execute():
            try:
                res = call_cloudflare_workers_ai_api(ocr_text, filename=filename)
                cloudflare_breaker.record_success()
                return res
            except Exception as e:
                cloudflare_breaker.record_failure()
                print(f"Cloudflare Workers AI API failed ({e}), trying fallback providers...", file=sys.stderr)
        return None

    def run_gemini():
        if gemini_breaker.can_execute():
            try:
                res = get_raw_gemini_response(ocr_text, filename=filename)
                gemini_breaker.record_success()
                return res
            except Exception as e:
                gemini_breaker.record_failure()
                print(f"Gemini API failed ({e}), trying fallback providers...", file=sys.stderr)
        return None

    providers = {
        "OpenRouter": run_openrouter,
        "Groq": run_groq,
        "Gemini": run_gemini,
        "Cloudflare": run_cloudflare
    }

    # Cheapest first; settings.primary_provider (env PRIMARY_PROVIDER) moves one provider to the front.
    primary = getattr(settings, "primary_provider", DEFAULT_PROVIDER_ORDER[0])
    execution_order = [p for p in [primary] if p in providers] + [p for p in DEFAULT_PROVIDER_ORDER if p != primary]

    for p_name in execution_order:
        result = providers[p_name]()
        if result is not None:
            content, usage = result
            return content, usage, p_name

    raise RuntimeError("All LLM providers are offline or circuits are OPEN. Cannot extract document.")
