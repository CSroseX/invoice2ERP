import os
import sys

# Must be set before any native binaries (numpy, torch, paddle, fitz) are imported
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["FLAGS_use_onednn"] = "0"
os.environ["FLAGS_enable_pir_in_executor"] = "0"
os.environ["FLAGS_enable_pir_api"] = "0"

# Pre-import torch to safely register DLLs in Windows process space
try:
    import torch
except Exception:
    pass

try:
    import paddle.inference
    _orig_create_predictor = paddle.inference.create_predictor
    def _custom_create_predictor(config):
        try:
            config.disable_onednn()
            config.disable_mkldnn()
            if hasattr(config, "enable_new_ir"):
                config.enable_new_ir(False)
        except Exception:
            pass
        return _orig_create_predictor(config)
    paddle.inference.create_predictor = _custom_create_predictor
except Exception:
    pass

import argparse
import gc
import importlib.metadata
import time
from pathlib import Path

# Ensure workspace root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
from PIL import Image
import pymupdf as fitz

TARGET_PDFS = [
    "documents/DU-03.pdf",
    "documents/DU-05.pdf",
    "documents/INV-21.pdf",
    "documents/HLD-10.pdf",
    "documents/INV-37.pdf",
]

_paddle_ocr_instance = None


def get_paddle_ocr():
    global _paddle_ocr_instance
    if _paddle_ocr_instance is None:
        from paddleocr import PaddleOCR
        try:
            _paddle_ocr_instance = PaddleOCR(
                lang="en",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except Exception:
            try:
                _paddle_ocr_instance = PaddleOCR(lang="en")
            except Exception:
                _paddle_ocr_instance = PaddleOCR()
    return _paddle_ocr_instance


def render_pdf_page_to_image(page: fitz.Page, dpi: int = 150) -> Image.Image:
    """Render a single PyMuPDF page to a PIL RGB Image."""
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def get_package_version(pkg_name: str) -> str:
    """Safely fetch installed package version."""
    try:
        return importlib.metadata.version(pkg_name)
    except Exception:
        return "unknown"


def run_current_engine_pdf(pdf_path: str) -> str:
    pdf_stem = Path(pdf_path).stem
    existing_parsed = ROOT_DIR / "parsed_files" / f"{pdf_stem}.txt"
    if existing_parsed.exists():
        with open(existing_parsed, "r", encoding="utf-8") as f:
            return f.read()
    from src.ocr_engine import extract_text
    pages_text = extract_text(pdf_path)
    return "\n\n--- PAGE BREAK ---\n\n".join(pages_text)


def run_paddle_engine_pdf(pdf_path: str) -> str:
    ocr = get_paddle_ocr()
    doc = fitz.open(pdf_path)
    page_outputs = []
    total_pages = len(doc)
    
    for page_idx, page in enumerate(doc):
        print(f"  -> Paddle page {page_idx + 1}/{total_pages}...", flush=True)
        img = render_pdf_page_to_image(page)
        img_np = np.array(img)
        res_raw = ocr.ocr(img_np)
        
        lines = []
        if res_raw:
            page_res = res_raw[0] if isinstance(res_raw, list) and len(res_raw) > 0 else res_raw
            # Handle PaddleOCR 3.x OCRResult object or dict
            if isinstance(page_res, dict) or hasattr(page_res, "__getitem__"):
                try:
                    texts = page_res.get("rec_texts", []) if isinstance(page_res, dict) else page_res["rec_texts"]
                    scores = page_res.get("rec_scores", []) if isinstance(page_res, dict) else page_res["rec_scores"]
                    for t, s in zip(texts, scores):
                        lines.append(f"[{float(s):.2f}] {str(t).strip()}")
                except Exception:
                    pass
            # Fallback for PaddleOCR 2.x list format
            if not lines and isinstance(page_res, (list, tuple)):
                for line in page_res:
                    if isinstance(line, (list, tuple)) and len(line) >= 2:
                        bbox, text_info = line[0], line[1]
                        if isinstance(text_info, (list, tuple)) and len(text_info) >= 2:
                            text, confidence = text_info[0], text_info[1]
                            lines.append(f"[{float(confidence):.2f}] {str(text).strip()}")
                        else:
                            lines.append(str(text_info).strip())
                    elif hasattr(line, "text"):
                        lines.append(str(line.text).strip())
                    elif isinstance(line, dict) and "rec_text" in line:
                        lines.append(f"[{float(line.get('rec_score', 1.0)):.2f}] {str(line.get('rec_text', '')).strip()}")
                        
        page_outputs.append(f"--- PAGE {page_idx + 1} ---\n" + "\n".join(lines))
        del img, img_np
        gc.collect()
        
    doc.close()
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_surya_engine_pdf(pdf_path: str) -> str:
    # Ensure LLAMA_CPP_BINARY is set
    if "LLAMA_CPP_BINARY" not in os.environ:
        os.environ["LLAMA_CPP_BINARY"] = r"C:\llama.cpp\llama-server.exe"
        
    from surya.recognition import RecognitionPredictor
    from bs4 import BeautifulSoup
    
    rec_predictor = RecognitionPredictor()
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    page_outputs = []
    
    for page_idx, page in enumerate(doc):
        print(f"  -> Surya page {page_idx + 1}/{total_pages}...", flush=True)
        img = render_pdf_page_to_image(page, dpi=150)
        results = rec_predictor([img])
        
        lines = []
        if results and len(results) > 0:
            page_res = results[0]
            # Surya 0.22.1 returns blocks with HTML markup
            if hasattr(page_res, "blocks") and page_res.blocks:
                for block in page_res.blocks:
                    if hasattr(block, "html") and block.html:
                        block_text = BeautifulSoup(block.html, "html.parser").get_text("\n").strip()
                        if block_text:
                            lines.append(block_text)
                    elif hasattr(block, "text") and block.text:
                        lines.append(str(block.text).strip())
            # Legacy fallback
            elif hasattr(page_res, "text_lines") and page_res.text_lines:
                for text_line in page_res.text_lines:
                    lines.append(f"[{text_line.confidence:.2f}] {text_line.text}")
                    
        page_outputs.append(f"--- PAGE {page_idx + 1} ---\n" + "\n".join(lines))
        del img
        gc.collect()
        
    doc.close()
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_doctr_engine_pdf(pdf_path: str) -> str:
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor
    
    doc = DocumentFile.from_pdf(pdf_path)
    model = ocr_predictor(pretrained=True)
    result = model(doc)
    
    export = result.export()
    page_outputs = []
    for page_idx, page in enumerate(export.get("pages", [])):
        lines = []
        for block in page.get("blocks", []):
            for line in block.get("lines", []):
                line_str = " ".join([w.get("value", "") for w in line.get("words", [])])
                lines.append(line_str)
        page_outputs.append(f"--- PAGE {page_idx + 1} ---\n" + "\n".join(lines))
        
    del doc, model, result
    gc.collect()
    
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_docling_engine_pdf(pdf_path: str) -> str:
    from docling.document_converter import DocumentConverter
    converter = DocumentConverter()
    result = converter.convert(pdf_path)
    markdown_output = result.document.export_to_markdown()
    del converter, result
    gc.collect()
    return markdown_output


ENGINE_MAP = {
    "current": (run_current_engine_pdf, ["easyocr", "pymupdf", "torch"]),
    "paddle": (run_paddle_engine_pdf, ["paddleocr", "paddlepaddle"]),
    "paddleocr": (run_paddle_engine_pdf, ["paddleocr", "paddlepaddle"]),
    "surya": (run_surya_engine_pdf, ["surya-ocr", "torch"]),
    "doctr": (run_doctr_engine_pdf, ["python-doctr", "torch"]),
    "docling": (run_docling_engine_pdf, ["docling", "docling-core"]),
}


def main():
    parser = argparse.ArgumentParser(description="Run OCR benchmark sequentially for a single engine across 5 PDFs.")
    parser.add_argument("--engine", required=True, choices=list(ENGINE_MAP.keys()), help="OCR engine name")
    args = parser.parse_args()
    
    engine_key = args.engine.lower()
    engine_fn, pkg_list = ENGINE_MAP[engine_key]
    
    # Standardize engine output directory name
    folder_name = "paddle" if engine_key in ["paddle", "paddleocr"] else engine_key
    out_dir = Path("measurements/ocr_benchmark") / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"==================================================")
    print(f"  OCR BENCHMARK RUNNER")
    print(f"  Engine: {folder_name.upper()}")
    print(f"  Output Dir: {out_dir}")
    print(f"==================================================\n")
    
    results_summary = []
    run_info_lines = []
    
    run_info_lines.append(f"Engine: {folder_name}")
    run_info_lines.append(f"Python Version: {sys.version.split()[0]}")
    pkg_versions = ", ".join([f"{pkg}={get_package_version(pkg)}" for pkg in pkg_list])
    run_info_lines.append(f"Package Versions: {pkg_versions}")
    run_info_lines.append("-" * 50)
    
    total_start = time.time()
    
    for pdf_rel_path in TARGET_PDFS:
        pdf_path = ROOT_DIR / pdf_rel_path
        pdf_name = pdf_path.name
        txt_filename = f"{pdf_path.stem}.txt"
        txt_filepath = out_dir / txt_filename
        
        print(f"Processing [{pdf_name}]...")
        if not pdf_path.exists():
            err_msg = f"File not found: {pdf_path}"
            print(f"  -> FAILED: {err_msg}")
            with open(txt_filepath, "w", encoding="utf-8") as f:
                f.write(f"ERROR: {err_msg}\n")
            results_summary.append((pdf_name, "FAILED", 0.0, err_msg))
            run_info_lines.append(f"PDF: {pdf_name} | Status: FAILED | Time: 0.00s | Error: {err_msg}")
            continue
            
        pdf_start = time.time()
        try:
            output_text = engine_fn(str(pdf_path))
            pdf_elapsed = time.time() - pdf_start
            
            with open(txt_filepath, "w", encoding="utf-8") as f:
                f.write(output_text)
                
            print(f"  -> SUCCESS ({pdf_elapsed:.2f}s) saved to {txt_filename}")
            results_summary.append((pdf_name, "SUCCESS", pdf_elapsed, ""))
            run_info_lines.append(f"PDF: {pdf_name} | Status: SUCCESS | Time: {pdf_elapsed:.2f}s")
        except Exception as e:
            pdf_elapsed = time.time() - pdf_start
            err_msg = f"{type(e).__name__}: {e}"
            print(f"  -> FAILED ({pdf_elapsed:.2f}s): {err_msg}")
            with open(txt_filepath, "w", encoding="utf-8") as f:
                f.write(f"ERROR: {err_msg}\n")
            results_summary.append((pdf_name, "FAILED", pdf_elapsed, err_msg))
            run_info_lines.append(f"PDF: {pdf_name} | Status: FAILED | Time: {pdf_elapsed:.2f}s | Error: {err_msg}")
            
        # Explicit garbage collection after each PDF
        gc.collect()
        
    total_elapsed = time.time() - total_start
    run_info_lines.append("-" * 50)
    run_info_lines.append(f"Total Benchmark Execution Time: {total_elapsed:.2f}s")
    
    # Save run_info.txt
    run_info_file = out_dir / "run_info.txt"
    with open(run_info_file, "w", encoding="utf-8") as f:
        f.write("\n".join(run_info_lines) + "\n")
        
    print(f"\n==================================================")
    print(f"  SUMMARY FOR ENGINE: {folder_name.upper()}")
    print(f"==================================================")
    for pdf_name, status, el_time, err in results_summary:
        if status == "SUCCESS":
            print(f"  {pdf_name:<15} : SUCCESS ({el_time:.2f}s)")
        else:
            print(f"  {pdf_name:<15} : FAILED ({err})")
    print(f"Total Time: {total_elapsed:.2f}s")
    print(f"Run Info saved to: {run_info_file}")
    print(f"==================================================\n")


if __name__ == "__main__":
    main()
