# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the app:**
```bash
streamlit run app.py
```

**Install dependencies** (Python 3.12):
```bash
pip install -r requirements.txt           # read-only showcase
pip install -r requirements-full.txt      # full pipeline: EasyOCR + LLM clients
pip install -r requirements-benchmark.txt # extra OCR engines for tools/ocr_benchmark/ only
```

**Run the test suite:**
```bash
python -m pytest tests -v
```
Unit tests run offline. The live golden-file test (`tests/test_accuracy.py::test_golden_file_extraction`) is skipped unless an LLM provider key is configured, and also needs the OCR dependencies from `requirements-full.txt`.

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

1. **OCR & Layout** (`src/ocr_engine.py`) — PyMuPDF native extraction for digital PDFs; falls back to 300 DPI render + EasyOCR (the only supported OCR engine) for scanned documents. Caches extracted text in `parsed_files/<stem>.txt`.

2. **Segmentation** (`src/segmenter.py`) — Splits multi-document PDFs into individual sub-documents.

3. **Classification** (`src/classifier.py`) — Rule-based heuristics decide if a segment is a bookable payable (INVOICE / CREDIT_MEMO) or not; declined segments never reach the LLM.

4. **AI Extraction + Grounding** (`src/extractor.py`, `src/extraction/`, `src/grounding.py`) — `src/extractor.py` orchestrates; `src/extraction/providers.py` holds the LLM API calls, `postprocessing.py` the deterministic clean-up rules, `fallback.py` the regex extractor, `prompts.py` the system prompt. Routes OCR text to an LLM provider via `src/resilience.py` (circuit-breaker cascade, cheapest model first: OpenRouter → Cloudflare → Groq → Gemini; see `DEFAULT_PROVIDER_ORDER` in `src/extraction/providers.py`). After extraction, `grounding.py` verifies every extracted field appears verbatim in the source text; ungrounded fields are blanked rather than kept.

4b. **Reconciliation** (`src/reconciler.py`, called from `src/extractor.py`) — Gated line-item reconciliation: rewrites a line's `unit_price` when the document's header math is self-consistent (printed `subtotal + total_tax_amount == gross_total`) and that line's `quantity * unit_price` disagrees with its printed total. Also deduplicates charges (drops a header charge field like `excise_duties` when the same amount is stated as a header tax) and detects self-consistency gaps in payable structure. Improved OCR decimal parsing (`parse_dot_decimal`) handles ambiguous period usage (e.g. "28.031.70" as thousands+decimal separators). Grounding verification now skips reconciler-derived fields via optional `reconciliation_audit` parameter, documenting intentional derivations.

5. **Master Data Resolution** (`src/master_matcher.py`) — Fuzzy-matches extracted supplier/buyer names against `master_data/*.json` reference files (exact VAT/name match first, then ≥85% similarity via `difflib.SequenceMatcher`).

6. **Payload Assembly** — Resolved payable written to `output/<stem>.json`. `process_document_file` also appends one metadata-only record per document to `logs/audit.jsonl` (provider, model, token usage, outcome per segment, grounding/reconciliation counts, ERP pass) — never document values.

7. **ERP Oracle** (`erp.py`) — Deterministic recompute of the gross total from line items, taxes, discounts, and charges. The output `will_book_gross` must match the document's stated gross within $0.05 for a PASS verdict.

**Key constraint:** `erp.py` is the grading oracle — it must not be modified. All extraction work must satisfy its exact accounting formula.

**Current accuracy:** the committed `output/*.json` predates the reconciler and gives 27/52 payables passing the ERP oracle (`measurements/payables_baseline.csv`). Commit 2981e70 reports 31/52 after replaying reconciliation and grounding over those payables; the output files have not been regenerated since.

## Configuration

API keys are managed via `pydantic-settings` in `src/config.py`. Place keys in `.env`:
```
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
CLOUDFLARE_WORKERS_AI=...
CLOUDFLARE_ACCOUNT_ID=...
```

The `primary_provider` field in `AppSettings` (env `PRIMARY_PROVIDER`, default `OpenRouter`) moves one provider to the front of the cascade; the rest follow the cheapest-first order. Quota (HTTP 429) and missing-key errors skip retries and move straight to the next provider.

## Key Data Contracts

- **Input**: PDFs in `documents/`
- **Output**: `output/<stem>.json` with `{"file": str, "payables": [...], "declined": [...]}`
- **Payable schema**: Documented in `AUTODRAFT_SCHEMA.md`
- **Sample payload**: `sample_autodraft.json`
- **ERP pass condition**: `abs(erp_book(p)["will_book_gross"] - float(p["gross_total"])) < 0.05`

## Current State

The `app.py` Streamlit UI is in **read-only showcase mode** — live processing (background workers, sidebar controls) is intentionally disabled. The UI reads from pre-generated `output/*.json` files only. The full processing pipeline code exists in `_process_single_pdf()` but is short-circuited at the top of that function.

## Tools & Measurements

- `tools/measure_payables.py` — replays every payable in `output/*.json` through `erp_book()` offline and writes `measurements/payables_baseline.csv`. Use it to measure accuracy changes; it needs no API keys.
- `tools/diag_five.py` — prints `erp_book()` internals for named payables, for diagnosing failures.
- `tools/ocr_benchmark/` — OCR-engine benchmark harness; results in `measurements/ocr_benchmark/`, write-up in `docs/ocr_benchmark_and_project_roadmap.md`.
- `parsed_files/<stem>.txt` — cached OCR text, usable for offline tests without an OCR engine.

## Docker

`Dockerfile` is based on `python:3.12-slim`, needs no system packages, and runs as a non-root user with a Streamlit health check. Default target `showcase` (read-only app, `requirements.txt`); `--target full` adds the pipeline dependencies with CPU-only PyTorch. `docker-compose.yml` builds the showcase target.

## Master Data

Local reference datasets in `master_data/`: `suppliers.json`, `po_master.json`, `payment_terms.json`, `tax_master.json`, `chart_of_books.json`. Used by `MasterDataMatcher` for fuzzy resolution of supplier IDs, company codes, and PO references.
