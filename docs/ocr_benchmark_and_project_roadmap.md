# Invoice2ERP OCR Benchmark & Project Roadmap

## 1. High-Level Project Goal

The primary objective of this project is to build a robust, end-to-end **Invoice-to-ERP Ingestion Pipeline** that extracts structured tabular and key-value financial data from complex, multi-format PDF invoices (scanned, digital, multi-page, multi-lingual, and variable layout) with high precision and low latency.

### Key Benchmark Targets (5 Critical Edge-Case PDFs)
1. **`DU-03.pdf`**: Multi-page (12 pages) air waybill (AWB) and customs invoice separation.
2. **`DU-05.pdf`**: Missing unit price behavior verification (handling list price vs actual unit price).
3. **`INV-21.pdf`**: Multi-column tabular alignment (`Qty: 24 | Unit Price: 34.69 | Total: 407.95`).
4. **`HLD-10.pdf`**: Multi-column service row preservation and international/Estonian character recognition.
5. **`INV-37.pdf`**: Complex utility bill arithmetic, inline multipliers (`x 1 =`, `x 0.097958122 = $2.35`), and condenser water tonnage.

---

## 2. Summary of Work Done So Far

### A. Environment & Setup Harmonization
- **Python Environment**: Identified that `.venv` was Python 3.14 (incompatible with deep learning C-extensions), whereas `benchmark_env` is **Python 3.11.9** containing all pre-installed target libraries.
- **Windows C++ & DLL Isolation**: Resolved OpenMP symbol collisions and `torch\lib\shm.dll` `WinError 127` errors by enforcing deterministic import order and runtime flags (`KMP_DUPLICATE_LIB_OK="TRUE"`).

### B. Benchmark Harness Implementation
- Created [`tools/ocr_benchmark/run_engine.py`](../tools/ocr_benchmark/run_engine.py) to provide:
  - Isolated, sequential single-engine benchmarking.
  - Per-PDF memory management (`gc.collect()`), per-page progress tracking, and latency profiling.
  - Standardized output generation in `measurements/ocr_benchmark/<engine>/` with `run_info.txt`.

### C. Engine-Specific Fixes & Execution

1. **Baseline / Current Engine (`current`)**
   - Extracted baseline heuristic text layers for all 5 target PDFs.
   - **Total Runtime**: **0.20s** (Fast baseline).

2. **PaddleOCR (`paddle`)**
   - **Fixes Applied**:
     - Resolved PaddlePaddle 3.3.1 C++ PIR executor crash (`onednn_instruction.cc:118`) via predictor configuration override.
     - Resolved PaddleOCR 3.7.0 `OCRResult` dictionary structure extraction, preventing empty outputs.
     - Reduced default render resolution to 150 DPI for stable CPU inference.
   - **Status**: **Completed (100% Success across all 5 PDFs)**.

3. **docTR (`doctr`)**
   - Verified PyTorch detection (`FAST`) + recognition (`CRNN-VGG16`) models.
   - **Total Runtime**: **154.27s** (~2.5 minutes total).
   - **Status**: **Completed (100% Success across all 5 PDFs)**.

4. **Docling (`docling`)**
   - Executed deep neural layout pipeline (`Docling Layout Heron` + `EasyOCR` + `TableFormer`).
   - Successfully converted full multi-page document structures to structured Markdown.
   - **Total Runtime**: **1847.61s** (~30 minutes total).
   - **Status**: **Completed (100% Success across all 5 PDFs)**.

Surya OCR was evaluated for integration but has been **dropped** from the benchmark; it is not run or compared.

---

## 3. Current Benchmark Status Matrix

| Engine | Method / Architecture | 5-PDF Benchmark Status | Total Runtime | Output Folder |
| :--- | :--- | :---: | :---: | :--- |
| **`current`** | Baseline Text Extractor | ✅ Completed | 0.20s | [`measurements/ocr_benchmark/current/`](../measurements/ocr_benchmark/current) |
| **`paddle`** | PaddleOCR 3.7 (PP-OCRv6 Det + Rec) | ✅ Completed | 48670.42s* | [`measurements/ocr_benchmark/paddle/`](../measurements/ocr_benchmark/paddle) |
| **`doctr`** | docTR 1.1.0 (FAST + CRNN-VGG16) | ✅ Completed | 154.27s | [`measurements/ocr_benchmark/doctr/`](../measurements/ocr_benchmark/doctr) |
| **`docling`** | Layout Heron + TableFormer + EasyOCR | ✅ Completed | 1847.61s | [`measurements/ocr_benchmark/docling/`](../measurements/ocr_benchmark/docling) |

\* Paddle's total is dominated by one outlier: `run_info.txt` records 46728.52s (~13 h) for `HLD-10.pdf` alone, versus 46–1567s for the other four PDFs. This looks like a stalled or suspended run rather than real inference cost and should be re-measured before Paddle's latency is compared.

---

## 4. Next High-Level Steps

### Step 1: Comparative Quality & Accuracy Analysis
- Perform a systematic evaluation across the 5 target PDFs comparing:
  1. **Character Error Rate (CER) / Word Accuracy**: Precision of amounts, dates, and item codes.
  2. **Table & Column Integrity**: Whether line-item columns (Qty, Unit Price, Tax, Line Total) remain aligned or collapse into single strings.
  3. **Multi-page & AWB Handling (`DU-03`)**: Separation of shipping headers vs invoice items.
  4. **Formula & Calculation Preservation (`INV-37`)**: Retaining multipliers and unit rates without distortion.

### Step 2: Architecture Decision & Production Recommendation
- Synthesize findings into a final architectural decision matrix:
  - **Latency vs Quality Trade-off**: Balancing runtime (docTR ~2.5 min vs Docling ~30 min) against structural table understanding.
  - **Selected Engine**: Select the optimal engine (or hybrid strategy: fast text-layer extraction with selective deep OCR fallback).

### Step 3: Pipeline Integration
- Integrate the selected engine into the ingestion workflow (`src/extractor.py`, `src/ocr_engine.py`) ensuring zero regression on clean digital PDFs and robust recovery on complex/scanned invoices.
