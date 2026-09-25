# 🏛️ invoice2ERP System Architecture & Technical Design

## 1. Executive Summary

**invoice2ERP** is designed around a core realization in financial document processing: **document extraction is not document booking**. 

A naive pipeline that reads invoice text and maps key-value pairs to JSON might achieve high visual fidelity, yet fail completely when downstream ERP software attempts to calculate ledger totals. Downstream ERP systems (e.g. SAP, Oracle, Zycus, NetSuite) compute gross obligations strictly from the raw itemized components provided. If line taxes are migrated to the header level, discounts are folded into unit prices, or unprinted figures are derived to force math to balance, the ledger booking fails or violates audit compliance.

invoice2ERP enforces an accounting-first architecture: **extract only what is grounded on the page, decompose all components, resolve master reference codes deterministically, and verify every payload against an ERP oracle before booking.**

---

## 2. Pipeline Execution Phases

```
┌────────────────────────────────────────────────────────────────────────┐
│                        invoice2ERP Architecture                        │
└────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 1: PDF → Spatial Layout OCR Engine         │
           │ Preserves 2D layout & bounding-box statistics   │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 2: Multi-Document PDF Pre-Segmenter       │
           │ Splits bundled multi-page PDFs into sub-docs    │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 3: Document Classifier                    │
           │ Payable (Invoice/Credit Memo) vs. Declined      │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 4: Multi-LLM Extraction Cascade           │
           │ OpenRouter -> Groq -> Cloudflare -> Gemini      │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 4b: Anti-Hallucination Grounding Engine   │
           │ Rule-1 verification against layout text tokens  │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 5: Master Data Resolution Engine          │
           │ Fuzzy matching & token overlap for ID codes     │
           └─────────────────────────────────────────────────┘
                                    │
                                    ▼
           ┌─────────────────────────────────────────────────┐
           │ Phase 6: Pre-ERP Payload Assembly & Oracle Check│
           │ Verifies cent-exact match via erp_book()        │
           └─────────────────────────────────────────────────┘
```

### Phase 1: Spatial Layout OCR Engine ([src/ocr_engine.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/ocr_engine.py))
- Detects whether a PDF page contains native vector text or requires raster rendering.
- For digital PDFs, extracts word bounding boxes directly using PyMuPDF (`fitz`).
- For scanned pages, renders high-resolution images at 300 DPI and invokes EasyOCR or PaddleOCR.
- Reconstructs a scale-invariant 2D layout text (`reconstruct_layout`) by calculating dynamic median line heights and character widths.

### Phase 2: Multi-Document PDF Pre-Segmenter ([src/segmenter.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/segmenter.py))
- Invoices are frequently received as multi-page PDFs containing bundled documents.
- Evaluates page header patterns (`PAGE_START_PATTERNS` like "Page 1 of N", "Page 1/1") and sub-document title patterns.
- Tracks document identifier continuity (e.g. PO/SO number overlap across page boundaries) to distinguish multi-page continuation pages from separate sub-documents.

### Phase 3: Document Classifier ([src/classifier.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/classifier.py))
- Evaluates each sub-document segment using a weighted numerical scoring model (`compute_payable_score`).
- Positive scoring factors (+10 to +40): Tax invoice headers, tabular item structures, tax IDs (VAT, MwSt, GST), buyer/seller party blocks, and financial total keywords.
- Negative scoring factors (-100): Non-payable titles (Quotes, Mahnung / Reminders, Delivery Notes, Packing Slips, Donation Requests).
- Special handling for `CREDIT_MEMO`: classified as a bookable payable with all monetary figures extracted as positive magnitudes.

### Phase 4: Multi-Provider LLM Extraction Cascade ([src/extractor.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/extractor.py))
- System prompt instructs LLM models to conform strictly to `AUTODRAFT_SCHEMA.md`.
- Implements automated provider fallback: **OpenRouter $\rightarrow$ Groq $\rightarrow$ Cloudflare Workers AI $\rightarrow$ Gemini**.
- Includes post-processing routines:
  - `apply_currency_stripping`: Normalizes currency symbols ($€, $, £, ¥, ₹$) and ISO codes without losing captured currency metadata.
  - `apply_locale_decimal_parsing`: Converts European comma-decimals (`1.628,16` $\rightarrow$ `1628.16`) and space-separated thousands.
  - `deduplicate_tax_placement`: Prevents tax duplication between line item taxes and header tax summary blocks.

### Phase 4b: Anti-Hallucination Grounding Engine ([src/grounding.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/grounding.py))
- **Rule 1 Enforcement**: Every emitted value must explicitly appear on the document.
- Audits header fields (`invoice_number`, `gross_total`, `subtotal`, `total_tax_amount`, `po_number`), line items, and tax objects against raw layout text.
- If a value cannot be grounded in the source text, it is blanked out and logged as a warning.

### Phase 5: Master Data Resolution Engine ([src/master_matcher.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/src/master_matcher.py))
- **Supplier Matching**: Matches VAT ID exactly or runs `SequenceMatcher` fuzzy similarity against known supplier names with a strict $\ge 85\%$ threshold.
- **Chart of Books Matching**: Tokenizes document text and matches against company, business unit, and location reference addresses in `master_data/chart_of_books.json`.
- **Payment Terms Resolution**: Resolves terms via alias text matching or date-delta calculation (`due_date - invoice_date`).
- **Tax Code Matching**: Resolves tax codes via country code and tax rate lookup in `master_data/tax_master.json`.

### Phase 6 & 7: ERP Oracle Booking Verification ([erp.py](file:///c:/Users/chitr/Desktop/coding/Zycus%20Assignment/candidate_kit/erp.py))
- The ERP oracle calculates:
  $$\text{Line Base} = \text{round}_2(\text{qty} \times \text{unit\_price} - \text{line\_discount})$$
  $$\text{Net Base} = \sum \text{Line Bases} - \text{Header Discount}$$
  $$\text{Gross} = \text{round}_2(\text{Net Base} + \sum \text{Line Taxes} + \sum \text{Header Taxes} + \text{Other Charges})$$
- Verifies that the recomputed gross matches the document's printed gross within a $0.05$ currency threshold.

---

## 3. Generalization & Reliability Guarantees

1. **Zero Backward-Derivation**: If a unit price is unprinted on a line item, invoice2ERP leaves the field blank. It never divides line total by quantity to invent a price, avoiding ungrounded hallucinations.
2. **Honest Gaps vs. False Matches**: In master data resolution, if similarity is below the $85\%$ confidence threshold, the reference ID code remains blank (`""`).
3. **Scale-Invariant Layout Analysis**: Coordinates and text layout grouping use DPI-independent median bounding box calculations, enabling robust handling of varied document formats.
