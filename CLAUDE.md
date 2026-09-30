# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the app:**
```bash
streamlit run app.py
```

**Install dependencies:**
```bash
pip install -r requirements.txt
# For full pipeline (OCR, ML deps):
pip install -r requirements-full.txt
```

**Run the regression test suite:**
```bash
python -m pytest tests/test_accuracy.py -v
```

**Run the ERP oracle directly on a JSON payload:**
```bash
python erp.py path/to/payable.json
```

**Environment setup:**
```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate  # Linux/macOS
cp .env.example .env     # Add your API keys
```

## Architecture

The pipeline runs in 7 sequential phases per document:

1. **OCR & Layout** (`src/ocr_engine.py`) — PyMuPDF native extraction for digital PDFs; falls back to 300 DPI render + EasyOCR for scanned documents. Caches extracted text in `parsed_files/<stem>.txt`.

2. **Segmentation** (`src/segmenter.py`) — Splits multi-document PDFs into individual sub-documents.

3. **Classification** (`src/classifier.py`) — Rule-based heuristics decide if a segment is a bookable payable (INVOICE / CREDIT_MEMO) or not; declined segments never reach the LLM.

4. **AI Extraction + Grounding** (`src/extractor.py`, `src/grounding.py`) — Routes OCR text to an LLM provider via `src/resilience.py` (circuit-breaker cascade: Groq → OpenRouter → Gemini → Cloudflare). After extraction, `grounding.py` verifies every extracted field appears verbatim in the source text; ungrounded fields are blanked rather than kept.

4b. **Reconciliation** (`src/reconciler.py`, called from `src/extractor.py`) — Gated line-item reconciliation: rewrites a line's `unit_price` when the document's header math is self-consistent (printed `subtotal + total_tax_amount == gross_total`) and that line's `quantity * unit_price` disagrees with its printed total. Also deduplicates charges (drops a header charge field like `excise_duties` when the same amount is stated as a header tax) and detects self-consistency gaps in payable structure. Improved OCR decimal parsing (`parse_dot_decimal`) handles ambiguous period usage (e.g. "28.031.70" as thousands+decimal separators). Grounding verification now skips reconciler-derived fields via optional `reconciliation_audit` parameter, documenting intentional derivations.

5. **Master Data Resolution** (`src/master_matcher.py`) — Fuzzy-matches extracted supplier/buyer names against `master_data/*.json` reference files (≥85% similarity threshold via `rapidfuzz`).

6. **Payload Assembly** — Resolved payable written to `output/<stem>.json`.

7. **ERP Oracle** (`erp.py`) — Deterministic recompute of the gross total from line items, taxes, discounts, and charges. The output `will_book_gross` must match the document's stated gross within $0.05 for a PASS verdict.

**Key constraint:** `erp.py` is the grading oracle — it must not be modified. All extraction work must satisfy its exact accounting formula.

## Configuration

API keys are managed via `pydantic-settings` in `src/config.py`. Place keys in `.env`:
```
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
CLOUDFLARE_WORKERS_AI=...
CLOUDFLARE_ACCOUNT_ID=...
```

The `primary_provider` field in `AppSettings` controls which provider the circuit breaker tries first.

## Key Data Contracts

- **Input**: PDFs in `documents/`
- **Output**: `output/<stem>.json` with `{"file": str, "payables": [...], "declined": [...]}`
- **Payable schema**: Documented in `AUTODRAFT_SCHEMA.md`
- **Sample payload**: `sample_autodraft.json`
- **ERP pass condition**: `abs(erp_book(p)["will_book_gross"] - float(p["gross_total"])) < 0.05`

## Current State

The `app.py` Streamlit UI is in **read-only showcase mode** — live processing (background workers, sidebar controls) is intentionally disabled. The UI reads from pre-generated `output/*.json` files only. The full processing pipeline code exists in `_process_single_pdf()` but is short-circuited at the top of that function.

## Master Data

Local reference datasets in `master_data/`: `suppliers.json`, `po_master.json`, `payment_terms.json`, `tax_master.json`, `chart_of_books.json`. Used by `MasterDataMatcher` for fuzzy resolution of supplier IDs, company codes, and PO references.
