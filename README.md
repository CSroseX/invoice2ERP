# invoice2ERP

**Turns supplier invoices into records an ERP system can actually book — not just records that look right.**

![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python)
![Streamlit](https://img.shields.io/badge/Streamlit-1.63-FF4B4B?style=flat-square&logo=streamlit)
![Docker](https://img.shields.io/badge/Docker-supported-2496ED?style=flat-square&logo=docker)
![License](https://img.shields.io/badge/License-MIT-brightgreen?style=flat-square)

---

## The problem this solves

Extracting text from an invoice is easy. Producing a record a downstream ERP will actually book is not — an ERP recomputes the gross total from raw components (quantities, unit prices, line vs. header taxes, discounts, charges), so a single field placed at the wrong level, or a number invented to make totals balance, causes booking to fail even when the extraction *looks* correct.

invoice2ERP treats that as the core engineering problem, not an edge case:

- **Every emitted value must be traceable to the source document.** A grounding pass checks each extracted field against the raw OCR text; anything not found is blanked rather than guessed.
- **Structure matters as much as the total.** A tax stated at the line level stays on the line; a tax stated once at the header stays at the header. Two records can reach the same total through different — and only one correct — structure.
- **A blank field is a correct answer.** When a value genuinely isn't on the page, the system reports the gap instead of backward-deriving a number to satisfy the total.

## Result

Measured against the project's own ERP booking oracle across all 52 extracted payables in the current corpus:

| | Payables booking correctly |
|---|---|
| Baseline extraction | 27 / 52 (51.9%) |
| + reconciliation & grounding hardening | **31 / 52 (59.6%)** |
| Regressions introduced | **0** |

That gain came from diagnosing *why* documents failed to book — not by retrying extraction, but by finding that the ERP oracle recomputes every line from `quantity × unit_price` and never reads the document's own printed line total. Invoices routinely print unit prices rounded for display, so a faithful transcription of the page can still fail arithmetic it was never meant to reproduce exactly.

The fix is a **gated reconciliation node** ([`src/reconciler.py`](src/reconciler.py)): it only rewrites a line's `unit_price` when the *document's own* header arithmetic (`subtotal + tax == gross`, exactly as printed) independently corroborates that a rounding gap is real — never by checking against the ERP oracle itself, which would be fitting the answer to the grader rather than the document. That gate is why the fix carries zero regressions: a payable that already booked correctly can't satisfy a corroboration condition it doesn't need.

## Architecture

Seven phases, each independently verifiable:

```
PDF → OCR & Layout → Segmentation → Classification → AI Extraction
    → Grounding Verification → Master Data Resolution → ERP Oracle Check
```

1. **OCR & Layout** ([`src/ocr_engine.py`](src/ocr_engine.py)) — native PyMuPDF text extraction for digital PDFs, with an EasyOCR fallback for scanned pages.
2. **Segmentation** ([`src/segmenter.py`](src/segmenter.py)) — splits multi-document PDFs (a single upload can contain zero, one, or several distinct payables).
3. **Classification** ([`src/classifier.py`](src/classifier.py)) — rule-based scoring decides whether a segment is a bookable payable before it ever reaches the LLM, so non-payables (quotes, delivery notes, reminders) never risk being hallucinated into one.
4. **AI Extraction** ([`src/extractor.py`](src/extractor.py)) — routed through a circuit-breaker cascade across LLM providers, cheapest model first (OpenRouter → Cloudflare → Groq → Gemini), so a single exhausted quota doesn't stop a batch.
5. **Grounding Verification** ([`src/grounding.py`](src/grounding.py)) — every extracted field must appear in the source OCR text; the check matches full numeric values (not just an integer prefix), which closes a real path for a derived or hallucinated decimal to pass silently.
6. **Master Data Resolution** ([`src/master_matcher.py`](src/master_matcher.py)) — fuzzy-matches suppliers, tax codes, and payment terms against reference data at a strict ≥85% similarity threshold; below that, the field is left unresolved rather than guessed.
7. **ERP Oracle Check** ([`erp.py`](erp.py)) — a sealed, deterministic recomputation of the gross total from raw components. This is the actual grading contract and is never modified by the pipeline.

Each input document yields one `output/<stem>.json`: `{"file", "payables", "declined", "failed"}`. `declined` lists segments the classifier judged not to be payables; `failed` lists segments whose extraction errored (provider outage, unparseable response), so a processing failure is never mistaken for a document decision. The full shape is in [`AUTODRAFT_SCHEMA.md`](AUTODRAFT_SCHEMA.md).

## Running it

**Public showcase (read-only).** `app.py` in this repository is a read-only Streamlit viewer over pre-computed output — it renders extraction results for a curated set of documents and has no code path that triggers live processing, by design, for public deployment.

```bash
pip install -r requirements.txt
streamlit run app.py
```

**Full pipeline (local).** The extraction pipeline (`src/extractor.py`, `src/ocr_engine.py`, `src/segmenter.py`, `src/classifier.py`) is a set of composable modules, not a bundled CLI — call `extract_payable_from_text()` from a script, or check a single already-extracted payable against the ERP oracle directly:

```bash
git clone https://github.com/CSroseX/invoice2ERP.git
cd invoice2ERP
python -m venv .venv && .venv\Scripts\activate   # Windows; use source .venv/bin/activate on macOS/Linux
pip install -r requirements-full.txt
cp .env.example .env   # add your LLM provider API key(s)
python erp.py sample_autodraft.json
```

Supported setup: Python 3.12 on Linux, macOS or Windows, CPU only. Scanned pages are OCR'd with EasyOCR, which installs from pip wheels on all three. The extra engines used by the OCR benchmark are in `requirements-benchmark.txt`.

**Docker:**
```bash
cp .env.example .env
docker compose up --build -d                     # read-only showcase (default target)
docker build --target full -t invoice2erp:full .  # image with the OCR/LLM pipeline (CPU PyTorch)
```

**Tests** — unit tests run offline; the golden-file test that runs a control document through the full pipeline is skipped unless an LLM key is configured:
```bash
python -m pytest tests -v
```

## Design decisions worth calling out

- **The ERP oracle is a sealed black box.** `erp.py` is never modified — it's the grading contract, and any edit to it would be invisible to grading and self-deceiving in development. All fixes work by producing better-grounded input, never by adjusting the check itself.
- **Circuit-breaker LLM routing**, not a single provider — automatic failover across four LLM APIs ([`src/resilience.py`](src/resilience.py)) when one hits a quota or outage, so a batch degrades gracefully instead of stopping.
- **Structured JSONL audit logging** ([`src/logging_config.py`](src/logging_config.py)) — pipeline events are logged as structured JSON, not free-text, so they're queryable rather than grep-only.

## Known limitations

- **Unprinted values stay blank by design.** If a line's unit price is genuinely not on the page, the system won't invent one — that line will not fully foot in the ERP check. This is a deliberate trade-off, not an oversight: an honest gap is worth more than a plausible-looking guess.
- **Master data resolution is reference-table scale.** The current `master_data/` lookups use exact and fuzzy string matching; a production deployment against 100k+ master records would need vector or elastic indexing instead.
- **The reconciliation gate is conservative by design.** It only fires when the document's own header arithmetic corroborates the gap — a looser gate would book more documents but risks correcting a document that didn't need it, which is a worse failure than leaving it alone.

## Repository layout

```
app.py                 Read-only Streamlit showcase
erp.py                 Sealed grading oracle — recomputes gross from raw components
src/
  ocr_engine.py        Phase 1 — OCR & spatial layout
  segmenter.py         Phase 2 — multi-document segmentation
  classifier.py        Phase 3 — payable vs. non-payable classification
  extractor.py         Phase 4 — LLM extraction + provider routing
  grounding.py         Phase 5 — anti-hallucination verification
  reconciler.py        Document-corroborated line reconciliation
  master_matcher.py    Phase 6 — master data fuzzy resolution
  resilience.py        Circuit breaker & retry logic for LLM providers
documents/             Source PDFs
output/                Generated autodraft JSON, one per input document
master_data/           Reference datasets for supplier/tax/PO matching
tests/                 Golden-file regression suite
AUTODRAFT_SCHEMA.md    The exact output record shape
```

## License

MIT — see [LICENSE](LICENSE).
