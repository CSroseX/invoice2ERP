"""Offline tests for retrying truncated LLM output on other providers (issue #9)."""
import json

import pytest

import src.extraction.providers as providers
import src.extractor as extractor
from erp import erp_book

OCR_TEXT = "Invoice INV-42\nWidget 2 x 5.00 = 10.00\nGadget 1 x 3.00 = 3.00\nTotal 13.00"
CLEAN_JSON = json.dumps({
    "invoice_number": "INV-42", "gross_total": "13.00", "taxes": [],
    "line_items": [
        {"description": "Widget", "quantity": "2", "unit_price": "5.00", "total": "10.00"},
        {"description": "Gadget", "quantity": "1", "unit_price": "3.00", "total": "3.00"},
    ],
})
# Cut off mid-array: repairable, but the second line item is lost.
TRUNCATED_JSON = CLEAN_JSON[: CLEAN_JSON.index(', {"description": "Gadget"')]


def _fake_cascade(monkeypatch, responses):
    """Replace the cascade with one that serves `responses` ({provider: raw_json}) in order,
    honouring `exclude`, and records each call's exclude list."""
    calls = []

    def fake(ocr_text, filename="", exclude=()):
        calls.append(list(exclude))
        for name, raw in responses.items():
            if name not in exclude:
                return raw, {"total_tokens": 5}, name
        raise RuntimeError("All LLM providers are offline or circuits are OPEN. Cannot extract document.")

    monkeypatch.setattr(extractor, "get_raw_llm_response", fake)
    monkeypatch.setattr(extractor, "has_openrouter_key", lambda: True)
    return calls


def test_truncated_then_clean_uses_second_provider(monkeypatch):
    calls = _fake_cascade(monkeypatch, {"OpenRouter": TRUNCATED_JSON, "Cloudflare": CLEAN_JSON})
    audit = {}

    payable = extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf", audit=audit)

    assert calls == [[], ["OpenRouter"]]
    assert len(payable["line_items"]) == 2
    assert "__review__" not in payable
    assert audit["provider"] == "Cloudflare"
    assert audit["providers_tried"] == ["OpenRouter", "Cloudflare"]
    assert audit["json_repair_retries"] == 1
    assert audit["json_repaired"] is False
    assert audit["needs_review"] is False


def test_all_truncated_keeps_repaired_payable_and_flags_review(monkeypatch):
    calls = _fake_cascade(monkeypatch, {"OpenRouter": TRUNCATED_JSON, "Cloudflare": TRUNCATED_JSON,
                                        "Groq": TRUNCATED_JSON})
    audit = {}

    payable = extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf", audit=audit)

    assert calls == [[], ["OpenRouter"], ["OpenRouter", "Cloudflare"], ["OpenRouter", "Cloudflare", "Groq"]]
    assert payable["invoice_number"] == "INV-42"
    assert len(payable["line_items"]) == 1
    assert payable["__review__"] == {
        "needed": True,
        "reasons": ["llm_response_truncated: repaired JSON after 3 providers"],
    }
    assert audit["provider"] == "OpenRouter"  # the first repaired response is kept
    assert audit["json_repaired"] is True
    assert audit["needs_review"] is True
    assert audit["json_repair_retries"] == 2
    erp_book(payable)  # the oracle ignores the metadata key


def test_clean_first_response_makes_a_single_call(monkeypatch):
    calls = _fake_cascade(monkeypatch, {"OpenRouter": CLEAN_JSON, "Cloudflare": CLEAN_JSON})
    audit = {}

    payable = extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf", audit=audit)

    assert calls == [[]]
    assert "__review__" not in payable
    assert audit["providers_tried"] == ["OpenRouter"]
    assert audit["json_repair_retries"] == 0
    assert audit["needs_review"] is False


def test_unrepairable_responses_still_fail(monkeypatch):
    _fake_cascade(monkeypatch, {"OpenRouter": "not json at all"})

    with pytest.raises(RuntimeError, match="LLM API Call Failed"):
        extractor.extract_payable_from_text(OCR_TEXT, filename="t.pdf")


def test_cascade_skips_excluded_providers(monkeypatch):
    called = []
    for name, func in [("OpenRouter", "call_openrouter_api"), ("Groq", "call_groq_api"),
                       ("Gemini", "get_raw_gemini_response"), ("Cloudflare", "call_cloudflare_workers_ai_api")]:
        def fail(ocr_text, filename="", _name=name):
            called.append(_name)
            raise RuntimeError("down")
        monkeypatch.setattr(providers, func, fail)
    for key, value in [("open_router_api_key", "sk"), ("groq_api_key", "gsk_x"), ("cloudflare_workers_ai_key", "cf")]:
        monkeypatch.setattr(providers.settings, key, value)
    monkeypatch.setattr(providers.settings, "primary_provider", "OpenRouter")
    for breaker in (providers.openrouter_breaker, providers.groq_breaker,
                    providers.cloudflare_breaker, providers.gemini_breaker):
        monkeypatch.setattr(breaker, "state", "CLOSED")
        monkeypatch.setattr(breaker, "failures", 0)

    with pytest.raises(RuntimeError, match="All LLM providers"):
        providers.get_raw_llm_response("text", exclude=["OpenRouter", "Groq"])

    assert called == ["Cloudflare", "Gemini"]
