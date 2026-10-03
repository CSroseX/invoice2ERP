"""Tests for the per-file output contract of process_document_file (issue #8)."""
import json

import src.extractor as extractor

INVOICE_TEXT = "INVOICE INV-777\nSupplier Secretco GmbH\nQty 2 Unit 50.00 Total 100.00\nTotal 100.00 EUR"


def _write(tmp_path, text):
    doc = tmp_path / "DOC-1.txt"
    doc.write_text(text, encoding="utf-8")
    return doc


def _silence_audit(monkeypatch):
    import logging
    logger = logging.getLogger("test.output_contract")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    monkeypatch.setattr(extractor, "get_audit_logger", lambda name: logger)


def test_extraction_failure_goes_to_failed_not_declined(monkeypatch, tmp_path):
    _silence_audit(monkeypatch)

    def boom(*args, **kwargs):
        try:
            raise ConnectionError("HTTP 500 - echoed request: IBAN DE89370400440532013000 Secretco")
        except ConnectionError as err:
            raise RuntimeError(f"LLM API Call Failed: {err}") from err

    monkeypatch.setattr(extractor, "extract_payable_from_text", boom)

    result = extractor.process_document_file(_write(tmp_path, INVOICE_TEXT))

    assert set(result) == {"file", "payables", "declined", "failed"}
    assert result["payables"] == []
    assert result["declined"] == []
    assert len(result["failed"]) == 1
    entry = result["failed"][0]
    assert entry["segment"] == 1
    assert entry["doc_type"] == "INVOICE"
    assert entry["error_type"] == "ConnectionError"
    assert "ConnectionError" in entry["reason"]
    # The raw provider message is never copied into the output.
    dumped = json.dumps(result)
    assert "DE89370400440532013000" not in dumped
    assert "Secretco" not in dumped
    assert "HTTP 500" not in dumped


def test_successful_document_has_empty_failed_list(monkeypatch, tmp_path):
    _silence_audit(monkeypatch)
    monkeypatch.setattr(extractor, "extract_payable_from_text",
                        lambda *a, **k: {"invoice_number": "INV-777", "gross_total": "100.00", "line_items": []})

    result = extractor.process_document_file(_write(tmp_path, INVOICE_TEXT))

    assert len(result["payables"]) == 1
    assert result["declined"] == []
    assert result["failed"] == []


def test_classifier_decline_stays_in_declined(monkeypatch, tmp_path):
    _silence_audit(monkeypatch)
    monkeypatch.setattr(extractor, "extract_payable_from_text",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not extract")))

    result = extractor.process_document_file(
        _write(tmp_path, "DELIVERY NOTE\nLieferschein 123\nPacking slip, no amounts due")
    )

    assert result["payables"] == []
    assert result["failed"] == []
    assert len(result["declined"]) == 1
    assert "Extraction failed" not in result["declined"][0]["reason"]
