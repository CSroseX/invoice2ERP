"""Tests for the audit trail written by process_document_file (src/logging_config.py)."""
import io
import json
import logging

import src.extraction.providers as providers
import src.extractor as extractor
import src.logging_config as logging_config

DOC_TEXT = "INVOICE INV-777\nSupplier Secretco GmbH IBAN DE89370400440532013000\nQty 2 Unit 50.00 Total 100.00\nTotal 100.00 EUR"
LLM_JSON = json.dumps({
    "invoice_number": "INV-777", "gross_total": "100.00", "subtotal": "100.00", "currency": "EUR",
    "line_items": [{"quantity": "2", "unit_price": "50.00", "total": "100.00"}], "taxes": [],
})


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_audit(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config.JsonFormatter())
    logger = logging.getLogger("test.audit")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    monkeypatch.setattr(extractor, "get_audit_logger", lambda name: logger)
    return stream


def _fake_llm(monkeypatch, body=LLM_JSON):
    payload = json.dumps({"choices": [{"message": {"content": body}}], "usage": {"total_tokens": 11}})
    monkeypatch.setattr(providers.settings, "open_router_api_key", "sk-test")
    monkeypatch.setattr(providers.settings, "primary_provider", "OpenRouter")
    monkeypatch.setattr(providers.openrouter_breaker, "state", "CLOSED")
    monkeypatch.setattr(providers.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(payload.encode("utf-8")))


def test_one_metadata_record_per_document(monkeypatch, tmp_path):
    stream = _capture_audit(monkeypatch)
    _fake_llm(monkeypatch)
    doc = tmp_path / "INV-777.txt"
    doc.write_text(DOC_TEXT, encoding="utf-8")

    result = extractor.process_document_file(doc)

    assert len(result["payables"]) == 1
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "document_processed"
    assert record["document_filename"] == "INV-777.pdf"
    assert record["status"] == "ok"
    assert record["trace_id"]
    seg = record["segments"][0]
    assert seg["outcome"] == "payable"
    assert seg["provider"] == "OpenRouter"
    assert seg["model"] == providers.settings.open_router_model
    assert seg["tokens"] == {"total_tokens": 11}
    assert seg["json_repaired"] is False
    assert seg["erp_pass"] is True
    # Metadata only: no document values in the audit trail.
    for secret in ("INV-777\"", "Secretco", "DE89370400440532013000", "100.00"):
        assert secret not in lines[0]


def test_failed_extraction_records_error_type_only(monkeypatch, tmp_path):
    stream = _capture_audit(monkeypatch)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDER_ORDER", ("OpenRouter",))
    monkeypatch.setattr(providers.settings, "open_router_api_key", "sk-test")
    monkeypatch.setattr(providers.openrouter_breaker, "state", "OPEN")
    monkeypatch.setattr(providers.openrouter_breaker, "last_failure_time", 9e18)
    doc = tmp_path / "INV-777.txt"
    doc.write_text(DOC_TEXT, encoding="utf-8")

    extractor.process_document_file(doc)

    record = json.loads(stream.getvalue().strip())
    assert record["status"] == "failed"
    assert record["segments"][0]["outcome"] == "failed"
    assert record["segments"][0]["error_type"] == "RuntimeError"


def test_audit_logger_writes_jsonl_file(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_config, "log_dir", tmp_path / "logs")
    logger = logging_config.get_audit_logger("test.audit.file")
    try:
        logger.info("hello", extra={"audit": {"event": "x"}})
        for h in logger.handlers:
            h.flush()
        line = (tmp_path / "logs" / "audit.jsonl").read_text(encoding="utf-8").strip()
        assert json.loads(line)["event"] == "x"
    finally:
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
