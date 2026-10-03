"""Offline tests for provider configuration and request handling (src/extraction/providers.py)."""
import io
import json
import logging

import pytest

import src.extraction.providers as providers
import src.extractor as extractor
from src.config import AppSettings, has_cloudflare_key, has_gemini_key, has_groq_key, has_openrouter_key


def _settings(**keys):
    return AppSettings.model_construct(**{
        "groq_api_key": "", "open_router_api_key": "", "gemini_api_key": "",
        "cloudflare_workers_ai_key": "", **keys,
    })


@pytest.mark.parametrize("check, field, good, bad", [
    (has_groq_key, "groq_api_key", "gsk_abc", ["", "<your-groq-key>", "abc"]),
    (has_openrouter_key, "open_router_api_key", "sk-or-abc", ["", "<your-key>"]),
    (has_cloudflare_key, "cloudflare_workers_ai_key", "cf-abc", ["", "<your-key>"]),
    (has_gemini_key, "gemini_api_key", "AIza-abc", ["", "<your-key>", "lang-client-xyz"]),
])
def test_key_checks(check, field, good, bad):
    assert check(_settings(**{field: good}))
    for value in bad:
        assert not check(_settings(**{field: value}))


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(calls):
    body = json.dumps({"choices": [{"message": {"content": '{"invoice_number": "X"}'}}], "usage": {}})

    def urlopen(req, timeout=None):
        calls.append(timeout)
        return _FakeResponse(body.encode("utf-8"))
    return urlopen


@pytest.mark.parametrize("func, key_field, key", [
    ("call_groq_api", "groq_api_key", "gsk_test"),
    ("call_openrouter_api", "open_router_api_key", "sk-test"),
    ("call_cloudflare_workers_ai_api", "cloudflare_workers_ai_key", "cf-test"),
])
def test_requests_have_timeout_and_do_not_log_document_text(monkeypatch, caplog, capsys, func, key_field, key):
    calls = []
    monkeypatch.setattr(providers.settings, key_field, key)
    monkeypatch.setattr(providers.urllib.request, "urlopen", _fake_urlopen(calls))
    secret_text = "IBAN DE89370400440532013000 SECRET-SUPPLIER"

    with caplog.at_level(logging.DEBUG, logger="src.extraction.providers"):
        getattr(providers, func)(secret_text, filename="doc.pdf")

    assert calls == [providers.LLM_HTTP_TIMEOUT_SECONDS]
    captured = capsys.readouterr()
    assert "SECRET-SUPPLIER" not in captured.out + captured.err
    assert "SECRET-SUPPLIER" not in caplog.text
    assert "doc.pdf" in caplog.text  # request metadata is still logged


LLM_JSON = '{"invoice_number": "INV-42", "gross_total": "10.00", "line_items": [], "taxes": []}'
OCR_TEXT = "Invoice INV-42\nTotal 10.00"


def _chat_response(content):
    body = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 7}})
    return _FakeResponse(body.encode("utf-8"))


def test_extracted_llm_fields_survive_end_to_end(monkeypatch):
    """Regression: providers return (content, usage); the content must not be discarded."""
    monkeypatch.setattr(providers.settings, "open_router_api_key", "sk-test")
    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda req, timeout=None: _chat_response(LLM_JSON))

    payable = extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf")

    assert payable["invoice_number"] == "INV-42"


def test_groq_json_validation_retry_returns_content_and_usage(monkeypatch):
    responses = iter([
        providers.urllib.error.HTTPError(
            "https://api.groq.com", 400, "Bad Request", {}, io.BytesIO(b'{"error": "json_validate_failed"}')
        ),
        _chat_response("```json\n" + LLM_JSON + "\n```"),
    ])

    def urlopen(req, timeout=None):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(providers.settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)

    content, usage = providers.call_groq_api(OCR_TEXT)

    assert json.loads(content)["invoice_number"] == "INV-42"
    assert usage == {"total_tokens": 7}


def test_gemini_returns_content_and_usage(monkeypatch):
    from types import SimpleNamespace

    fake_client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: SimpleNamespace(text=LLM_JSON + "\n")
    ))
    monkeypatch.setattr(providers, "_gemini_client", fake_client)
    monkeypatch.setattr(providers, "types", SimpleNamespace(GenerateContentConfig=lambda **kwargs: None))

    content, usage = providers.get_raw_gemini_response(OCR_TEXT)

    assert json.loads(content)["invoice_number"] == "INV-42"
    assert usage == {}


def test_gemini_client_gets_request_timeout(monkeypatch):
    from types import SimpleNamespace

    built = []
    fake_genai = SimpleNamespace(Client=lambda **kwargs: built.append(kwargs) or "client")
    monkeypatch.setattr(providers, "genai", fake_genai)
    monkeypatch.setattr(providers, "types", SimpleNamespace(HttpOptions=lambda **kwargs: kwargs))
    monkeypatch.setattr(providers, "_gemini_client", None)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "AIza-test")

    assert providers._get_gemini_client() == "client"
    # google-genai takes the timeout in milliseconds.
    assert built[0]["http_options"] == {"timeout": providers.LLM_HTTP_TIMEOUT_SECONDS * 1000}


def _record_cascade(monkeypatch):
    called = []
    for name, func in [("OpenRouter", "call_openrouter_api"), ("Groq", "call_groq_api"),
                       ("Gemini", "get_raw_gemini_response"), ("Cloudflare", "call_cloudflare_workers_ai_api")]:
        def fail(ocr_text, filename="", _name=name):
            called.append(_name)
            raise RuntimeError("down")
        monkeypatch.setattr(providers, func, fail)
    for key, value in [("open_router_api_key", "sk"), ("groq_api_key", "gsk_x"), ("cloudflare_workers_ai_key", "cf")]:
        monkeypatch.setattr(providers.settings, key, value)
    for breaker in (providers.openrouter_breaker, providers.groq_breaker,
                    providers.cloudflare_breaker, providers.gemini_breaker):
        monkeypatch.setattr(breaker, "state", "CLOSED")
        monkeypatch.setattr(breaker, "failures", 0)
    return called


def test_cascade_tries_cheapest_provider_first(monkeypatch):
    called = _record_cascade(monkeypatch)
    monkeypatch.setattr(providers.settings, "primary_provider", "OpenRouter")

    with pytest.raises(RuntimeError, match="All LLM providers"):
        providers.get_raw_llm_response("text")

    assert called == ["OpenRouter", "Cloudflare", "Groq", "Gemini"]


def test_primary_provider_moves_to_front(monkeypatch):
    called = _record_cascade(monkeypatch)
    monkeypatch.setattr(providers.settings, "primary_provider", "Groq")

    with pytest.raises(RuntimeError, match="All LLM providers"):
        providers.get_raw_llm_response("text")

    assert called == ["Groq", "OpenRouter", "Cloudflare", "Gemini"]
