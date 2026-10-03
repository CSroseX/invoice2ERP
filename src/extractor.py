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
import time
import uuid
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
from erp import erp_book, num
from src.extraction.providers import _get_gemini_client, get_raw_llm_response, provider_model
from src.grounding import verify_payable_grounding
from src.logging_config import configure_console_logging, get_audit_logger
from src.master_matcher import MasterDataMatcher
from src.ocr_engine import extract_text
from src.reconciler import reconcile_payable
from src.segmenter import segment_document_text

load_dotenv(override=True)

logger = logging.getLogger(__name__)

_matcher = MasterDataMatcher()


def _get_parsed_llm_payable(ocr_text: str, filename: str) -> tuple[dict | None, dict, str, list]:
    """Call the provider cascade until one provider returns JSON that parses without repair.

    A response that only parses after repair_json_string() was usually cut off mid-output
    (a repaired truncation silently drops line items), so the same payload is retried on
    every provider not yet tried. Returns (payable, usage, provider, tried), where tried is
    [(provider, raw_json, usage), ...] in call order; payable is None if no provider
    returned clean JSON.
    """
    tried: list[tuple[str, str, dict]] = []
    while True:
        try:
            raw_json, usage, provider = get_raw_llm_response(
                ocr_text, filename=filename, exclude=[name for name, _, _ in tried]
            )
        except RuntimeError:
            if not tried:
                raise
            return None, {}, "", tried  # every remaining provider failed or none are left
        tried.append((provider, raw_json, usage))
        try:
            return json.loads(raw_json, strict=False), usage, provider, tried
        except Exception:
            logger.warning(
                "LLM response from %s for %s needs JSON repair; retrying on other providers",
                provider, filename or "DOCUMENT",
            )


def _repair_first(tried: list[tuple[str, str, dict]]) -> tuple[dict, dict, str]:
    """Parse the first response that repair_json_string() can turn into valid JSON."""
    error: Exception | None = None
    for provider, raw_json, usage in tried:
        try:
            return json.loads(repair_json_string(raw_json), strict=False), usage, provider
        except Exception as e:
            error = error or e
    raise error


def extract_payable_from_text(
    ocr_text: str, filename: str = "", allow_fallback: bool = False, audit: dict | None = None
) -> dict:
    """Extract structured autodraft JSON from OCR layout text using OpenRouter, Groq, Cloudflare, or Gemini API.

    If `audit` is a dict, it is filled with run metadata for the audit trail (provider, model,
    token usage, counts of grounding warnings and reconciliation changes). It never receives
    document values.
    """
    audit = audit if audit is not None else {}
    has_openrouter = has_openrouter_key()
    has_groq = has_groq_key()
    has_cloudflare = has_cloudflare_key()
    has_gemini = _get_gemini_client() is not None

    if has_openrouter or has_groq or has_cloudflare or has_gemini:
        try:
            payable_data, usage, provider, providers_tried = _get_parsed_llm_payable(ocr_text, filename)
            json_repaired = payable_data is None
            if json_repaired:
                # No provider returned clean JSON: keep the first repairable response, flag it for review.
                payable_data, usage, provider = _repair_first(providers_tried)
            logger.info("LLM usage for %s: %s", filename or "DOCUMENT", usage)
            audit.update(
                provider=provider, model=provider_model(provider), tokens=usage, json_repaired=json_repaired,
                providers_tried=[name for name, _, _ in providers_tried],
                json_repair_retries=len(providers_tried) - 1, needs_review=json_repaired,
            )
            payable_data = apply_currency_stripping(payable_data)
            payable_data = apply_locale_decimal_parsing(payable_data)
            payable_data = apply_fix3_and_fix4_postprocessing(payable_data, ocr_text)
            payable_data = deduplicate_tax_placement(payable_data)
            payable_data, reconciliation_audit = reconcile_payable(payable_data)
            grounded_payable, grounding_warnings = verify_payable_grounding(payable_data, ocr_text, reconciliation_audit)
            verify_structural_integrity(grounded_payable)
            audit.update(
                grounding_warnings=len(grounding_warnings),
                reconciliation_changes=len(reconciliation_audit.get("line_reconciliations", []))
                + len(reconciliation_audit.get("charge_dedup_warnings", [])),
            )
            if reconciliation_audit:
                grounded_payable["__reconciliation__"] = reconciliation_audit
            if json_repaired:
                grounded_payable["__review__"] = {
                    "needed": True,
                    "reasons": [
                        f"llm_response_truncated: repaired JSON after {len(providers_tried)} providers"
                    ],
                }
            return _matcher.resolve_payable(grounded_payable, text_context=ocr_text)
        except Exception as e:
            if not allow_fallback:
                raise RuntimeError(f"LLM API Call Failed: {e}") from e
            print(f"LLM API error ({e}). Explicit fallback allowed.", file=sys.stderr)

    if allow_fallback:
        payable_data = deterministic_extract_payable(ocr_text, filename=filename)
        grounded_payable, grounding_warnings = verify_payable_grounding(payable_data, ocr_text)
        audit.update(provider="deterministic_fallback", grounding_warnings=len(grounding_warnings))
        return _matcher.resolve_payable(grounded_payable, text_context=ocr_text)

    raise ValueError("No valid OPEN_ROUTER_API, settings.groq_api_key, CLOUDFLARE_WORKERS_AI or settings.gemini_api_key configured and allow_fallback=False.")


def _erp_pass(payable: dict) -> bool | None:
    """Whether the ERP oracle books the payable's printed gross (None if no gross was extracted)."""
    if not str(payable.get("gross_total") or "").strip():
        return None
    return abs(erp_book(payable)["will_book_gross"] - num(payable["gross_total"])) < 0.05


def process_document_file(pdf_or_txt_path: str | Path) -> dict:
    """Process a single document PDF or .txt into per-file Autodraft JSON payload with multi-document segmentation.

    Writes one metadata-only record per document to the audit trail (logs/audit.jsonl).
    """
    path = Path(pdf_or_txt_path)
    started = time.monotonic()

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
    failed = []
    segments = []

    for idx, seg_text in enumerate(subdoc_texts, 1):
        seg_file_label = f"{file_name}#subdoc{idx}" if len(subdoc_texts) > 1 else file_name
        class_res = classify_document_text(seg_text, filename=seg_file_label)
        seg_audit = {"segment": idx, "doc_type": class_res.doc_type}

        if class_res.is_payable:
            try:
                payable = extract_payable_from_text(seg_text, filename=seg_file_label, audit=seg_audit)
                if class_res.doc_type == "CREDIT_MEMO":
                    payable["invoice_type"] = "CREDIT_MEMO"
                payables.append(payable)
                seg_audit.update(outcome="payable", erp_pass=_erp_pass(payable))
            except Exception as e:
                print(f"Error extracting payable from {seg_file_label}: {e}", file=sys.stderr)
                # Error type only: provider error messages can echo request content.
                error_type = type(e.__cause__ or e).__name__
                failed.append({
                    "segment": idx,
                    "doc_type": class_res.doc_type,
                    "error_type": error_type,
                    "reason": f"Extraction failed ({error_type}); see the processing log for details",
                })
                seg_audit.update(outcome="failed", error_type=error_type)
        else:
            declined.append({"doc_type": class_res.doc_type, "reason": "; ".join(class_res.reasons)})
            seg_audit.update(outcome="declined")
        segments.append(seg_audit)

    outcomes = [s["outcome"] for s in segments]
    get_audit_logger("invoice2erp.audit").info(
        "document processed: %s payables=%d declined=%d failed=%d",
        file_name, outcomes.count("payable"), outcomes.count("declined"), outcomes.count("failed"),
        extra={
            "trace_id": uuid.uuid4().hex,
            "doc_filename": file_name,
            "status": "failed" if "failed" in outcomes else "ok",
            "audit": {
                "event": "document_processed",
                "duration_s": round(time.monotonic() - started, 3),
                "segments": segments,
            },
        },
    )

    return {
        "file": file_name,
        "payables": payables,
        "declined": declined,
        "failed": failed,
    }


if __name__ == "__main__":
    configure_console_logging()
    if len(sys.argv) < 2:
        print("Usage: python -m src.extractor <pdf_or_txt_path>", file=sys.stderr)
        sys.exit(1)

    target_file = Path(sys.argv[1])
    result = process_document_file(target_file)
    print(json.dumps(result, indent=2))
