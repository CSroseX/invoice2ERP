"""
extractor.py — Phase 4: Structured Auto-Draft Extraction via an LLM provider cascade.

Extracts structured header fields, line items, and taxes from OCR layout text
into autodraft JSON format complying strictly with AUTODRAFT_SCHEMA.md.

This module only orchestrates. The pieces live in src/extraction/:
- providers.py       LLM API calls (OpenRouter, Groq, Gemini, Cloudflare) and the cascade
- postprocessing.py  deterministic clean-up rules applied to the LLM output
- fallback.py        regex extractor used when no LLM is configured
- prompts.py         the system prompt

After extraction, the payable is reconciled (src/reconciler.py), grounded against the
OCR text (src/grounding.py) and resolved against master data (src/master_matcher.py).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.classifier import classify_document_text
from src.config import has_cloudflare_key, has_groq_key, has_openrouter_key
from src.extraction.fallback import deterministic_extract_payable
from src.extraction.postprocessing import (
    apply_currency_stripping,
    apply_fix3_and_fix4_postprocessing,
    apply_locale_decimal_parsing,
    deduplicate_tax_placement,
    repair_json_string,
    verify_structural_integrity,
)
from src.extraction.providers import _get_gemini_client, get_raw_llm_response
from src.grounding import verify_payable_grounding
from src.master_matcher import MasterDataMatcher
from src.ocr_engine import extract_text
from src.reconciler import reconcile_payable
from src.segmenter import segment_document_text

load_dotenv(override=True)

logger = logging.getLogger(__name__)

_matcher = MasterDataMatcher()


def extract_payable_from_text(ocr_text: str, filename: str = "", allow_fallback: bool = False) -> dict:
    """Extract structured autodraft JSON from OCR layout text using OpenRouter, Groq, Cloudflare, or Gemini API."""
    has_openrouter = has_openrouter_key()
    has_groq = has_groq_key()
    has_cloudflare = has_cloudflare_key()
    has_gemini = _get_gemini_client() is not None

    if has_openrouter or has_groq or has_cloudflare or has_gemini:
        try:
            raw_json, usage = get_raw_llm_response(ocr_text, filename=filename)
            logger.info("LLM usage for %s: %s", filename or "DOCUMENT", usage)
            try:
                payable_data = json.loads(raw_json, strict=False)
            except Exception:
                repaired_str = repair_json_string(raw_json)
                payable_data = json.loads(repaired_str, strict=False)
            payable_data = apply_currency_stripping(payable_data)
            payable_data = apply_locale_decimal_parsing(payable_data)
            payable_data = apply_fix3_and_fix4_postprocessing(payable_data, ocr_text)
            payable_data = deduplicate_tax_placement(payable_data)
            payable_data, reconciliation_audit = reconcile_payable(payable_data)
            grounded_payable, _ = verify_payable_grounding(payable_data, ocr_text, reconciliation_audit)
            verify_structural_integrity(grounded_payable)
            if reconciliation_audit:
                grounded_payable["__reconciliation__"] = reconciliation_audit
            return _matcher.resolve_payable(grounded_payable, text_context=ocr_text)
        except Exception as e:
            if not allow_fallback:
                raise RuntimeError(f"LLM API Call Failed: {e}") from e
            print(f"LLM API error ({e}). Explicit fallback allowed.", file=sys.stderr)

    if allow_fallback:
        payable_data = deterministic_extract_payable(ocr_text, filename=filename)
        grounded_payable, _ = verify_payable_grounding(payable_data, ocr_text)
        return _matcher.resolve_payable(grounded_payable, text_context=ocr_text)

    raise ValueError("No valid OPEN_ROUTER_API, settings.groq_api_key, CLOUDFLARE_WORKERS_AI or settings.gemini_api_key configured and allow_fallback=False.")


def process_document_file(pdf_or_txt_path: str | Path) -> dict:
    """Process a single document PDF or .txt into per-file Autodraft JSON payload with multi-document segmentation."""
    path = Path(pdf_or_txt_path)

    if path.suffix == ".pdf":
        pages = extract_text(str(path))
        full_ocr_text = "\n\n--- PAGE BREAK ---\n\n".join(pages)
        file_name = path.name
    else:
        full_ocr_text = path.read_text(encoding="utf-8", errors="ignore")
        file_name = path.stem + ".pdf"

    subdoc_texts = segment_document_text(full_ocr_text)

    payables = []
    declined = []

    for idx, seg_text in enumerate(subdoc_texts, 1):
        seg_file_label = f"{file_name}#subdoc{idx}" if len(subdoc_texts) > 1 else file_name
        class_res = classify_document_text(seg_text, filename=seg_file_label)

        if class_res.is_payable:
            try:
                payable = extract_payable_from_text(seg_text, filename=seg_file_label)
                if class_res.doc_type == "CREDIT_MEMO":
                    payable["invoice_type"] = "CREDIT_MEMO"
                payables.append(payable)
            except Exception as e:
                print(f"Error extracting payable from {seg_file_label}: {e}", file=sys.stderr)
                declined.append({"doc_type": class_res.doc_type, "reason": f"Extraction failed: {e}"})
        else:
            declined.append({"doc_type": class_res.doc_type, "reason": "; ".join(class_res.reasons)})

    return {
        "file": file_name,
        "payables": payables,
        "declined": declined
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.extractor <pdf_or_txt_path>", file=sys.stderr)
        sys.exit(1)

    target_file = Path(sys.argv[1])
    result = process_document_file(target_file)
    print(json.dumps(result, indent=2))
