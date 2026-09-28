import json
import pytest
from pathlib import Path
from src.extractor import extract_payable_from_text
from src.ocr_engine import extract_pages, extract_text, is_digital_vector_page

# Paths
GOLDEN_DIR = Path(__file__).parent / "golden_control"

def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

@pytest.mark.parametrize("pdf_filename", [
    "DU-02.pdf"
])
def test_golden_file_extraction(pdf_filename):
    """
    Golden File Regression Test.
    Runs the actual extraction pipeline on a control document and compares 
    the structured output against a known-good expected JSON payload.
    """
    pdf_path = GOLDEN_DIR / pdf_filename
    expected_path = GOLDEN_DIR / f"{pdf_path.stem}.expected.json"
    
    assert pdf_path.exists(), f"Missing control document: {pdf_path}"
    assert expected_path.exists(), f"Missing expected output: {expected_path}"
    
    expected_data = load_json(expected_path)
    
    # 1. Run OCR Extraction
    # We bypass caching here to ensure we test the raw OCR engine if needed, 
    # but for speed in CI we can just use extract_text
    ocr_text = extract_text(pdf_path)
    if isinstance(ocr_text, list):
        ocr_text = "\n".join(ocr_text)
    assert len(ocr_text) > 100, "OCR extraction failed or returned too little text"
    
    # 2. Run LLM Extraction (Real LLM Call)
    # This intentionally hits the LLM to verify prompt/model integrity
    payable = extract_payable_from_text(ocr_text, filename=pdf_filename, allow_fallback=True)
    
    # 3. Compare Outputs
    # Since LLMs are non-deterministic, we check critical structured fields
    # rather than a direct string-to-string match.
    
    # Check Header Fields
    assert payable.get("invoice_id") == expected_data.get("invoice_id")
    assert payable.get("invoice_date") == expected_data.get("invoice_date")
    assert payable.get("currency") == expected_data.get("currency")
    
    # Check Financials (These must match exactly or grounding failed)
    assert payable.get("gross_amount") == expected_data.get("gross_amount")
    assert payable.get("tax_amount") == expected_data.get("tax_amount")
    
    # Check Line Items count
    assert len(payable.get("line_items", [])) == len(expected_data.get("line_items", []))
    
    # Ensure token metadata was injected by our Cost Tracking patch
    tokens = payable.pop("__tokens__", None)
    assert tokens is not None, "Tokens metadata failed to inject into payload"
