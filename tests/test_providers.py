"""Offline tests for provider configuration and request handling in src/extractor.py."""
import io
import json
import logging

import pytest

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
    monkeypatch.setattr(extractor.settings, key_field, key)
    monkeypatch.setattr(extractor.urllib.request, "urlopen", _fake_urlopen(calls))
    secret_text = "IBAN DE89370400440532013000 SECRET-SUPPLIER"

    with caplog.at_level(logging.DEBUG, logger="src.extractor"):
        getattr(extractor, func)(secret_text, filename="doc.pdf")

    assert calls == [extractor.LLM_HTTP_TIMEOUT_SECONDS]
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
    monkeypatch.setattr(extractor.settings, "open_router_api_key", "sk-test")
    monkeypatch.setattr(extractor.urllib.request, "urlopen", lambda req, timeout=None: _chat_response(LLM_JSON))

    payable = extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf")

    assert payable["invoice_number"] == "INV-42"


def test_groq_json_validation_retry_returns_content_and_usage(monkeypatch):
    responses = iter([
        extractor.urllib.error.HTTPError(
            "https://api.groq.com", 400, "Bad Request", {}, io.BytesIO(b'{"error": "json_validate_failed"}')
        ),
        _chat_response("```json\n" + LLM_JSON + "\n```"),
    ])

    def urlopen(req, timeout=None):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(extractor.settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(extractor.urllib.request, "urlopen", urlopen)

    content, usage = extractor.call_groq_api(OCR_TEXT)

    assert json.loads(content)["invoice_number"] == "INV-42"
    assert usage == {"total_tokens": 7}


def test_gemini_returns_content_and_usage(monkeypatch):
    from types import SimpleNamespace

    fake_client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: SimpleNamespace(text=LLM_JSON + "\n")
    ))
    monkeypatch.setattr(extractor, "_gemini_client", fake_client)
    monkeypatch.setattr(extractor, "types", SimpleNamespace(GenerateContentConfig=lambda **kwargs: None))

    content, usage = extractor.get_raw_gemini_response(OCR_TEXT)

    assert json.loads(content)["invoice_number"] == "INV-42"
    assert usage == {}
