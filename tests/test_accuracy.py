import json
from pathlib import Path

import pytest

from src.classifier import classify_document_text
from src.config import has_cloudflare_key, has_groq_key, has_openrouter_key
from src.extraction.providers import _get_gemini_client
from src.extractor import process_document_file
from src.segmenter import segment_document_text

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).parent / "golden_control"
PARSED_DIR = ROOT / "parsed_files"

# Header fields that must match the golden payload exactly (the schema's real names).
HEADER_FIELDS = ["invoice_number", "invoice_date", "currency", "gross_total", "total_tax_amount"]


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _any_llm_configured() -> bool:
    return has_openrouter_key() or has_groq_key() or has_cloudflare_key() or _get_gemini_client() is not None


@pytest.mark.parametrize("stem", ["DU-02"])
def test_segmentation_and_classification_match_golden(stem):
    """Offline: the cached OCR text segments and classifies into the golden payable count.

    Uses parsed_files/<stem>.txt so it needs neither an OCR engine nor an LLM.
    """
    expected = load_json(GOLDEN_DIR / f"{stem}.expected.json")
    text = (PARSED_DIR / f"{stem}.txt").read_text(encoding="utf-8")

    segments = segment_document_text(text)
    payable_segments = [s for s in segments if classify_document_text(s).is_payable]

    assert len(payable_segments) == len(expected["payables"])


@pytest.mark.skipif(not _any_llm_configured(), reason="needs an LLM provider key in .env")
@pytest.mark.parametrize("pdf_filename", ["DU-02.pdf"])
def test_golden_file_extraction(pdf_filename):
    """Live golden-file regression test: runs the full pipeline (OCR + real LLM calls) on a
    control document and compares critical fields per payable against the expected output.

    LLMs are non-deterministic, so only critical structured fields are compared rather than
    the whole payload. No deterministic fallback is allowed, so an LLM failure fails the test.
    """
    pdf_path = GOLDEN_DIR / pdf_filename
    expected = load_json(GOLDEN_DIR / f"{pdf_path.stem}.expected.json")

    result = process_document_file(pdf_path)

    assert len(result["payables"]) == len(expected["payables"])
    for got, want in zip(result["payables"], expected["payables"]):
        for field in HEADER_FIELDS:
            assert got.get(field) == want.get(field), f"{want.get('invoice_number')}: {field}"
        assert len(got.get("line_items", [])) == len(want.get("line_items", []))
