"""
tools/ocr_benchmark/run_one.py

Sequential single-file, single-engine OCR benchmark runner.
Processes ONLY one engine and ONE PDF file at a time, then exits immediately.
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure workspace root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
from PIL import Image
import pymupdf as fitz


def render_pdf_pages_to_images(pdf_path: str, dpi: int = 300) -> list[Image.Image]:
    """Render PDF pages to PIL RGB Images."""
    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    return images


def run_current_engine(pdf_path: str) -> str:
    from src.ocr_engine import extract_text
    pages_text = extract_text(pdf_path)
    return "\n\n--- PAGE BREAK ---\n\n".join(pages_text)


def run_paddleocr(pdf_path: str) -> str:
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(use_angle_cls=True, lang="en", use_gpu=False, show_log=False)
    images = render_pdf_pages_to_images(pdf_path)
    page_outputs = []
    
    for page_idx, img in enumerate(images):
        img_np = np.array(img)
        result = ocr.ocr(img_np, cls=True)
        lines = []
        if result and result[0] is not None:
            for line in result[0]:
                bbox, (text, confidence) = line
                lines.append(f"[{confidence:.2f}] {text.strip()}")
        page_outputs.append(f"--- PAGE {page_idx + 1} ---\n" + "\n".join(lines))
        
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_surya(pdf_path: str) -> str:
    from surya.ocr import run_ocr
    from surya.model.detection.segformer import load_model as load_det_model, load_processor as load_det_processor
    from surya.model.recognition.model import load_model as load_rec_model
    from surya.model.recognition.processor import load_processor as load_rec_processor
    
    images = render_pdf_pages_to_images(pdf_path)
    det_model, det_processor = load_det_model(), load_det_processor()
    rec_model, rec_processor = load_rec_model(), load_rec_processor()
    
    predictions = run_ocr(images, [["en"]] * len(images), det_model, det_processor, rec_model, rec_processor)
    
    page_outputs = []
    for page_idx, pred in enumerate(predictions):
        lines = []
        for text_line in pred.text_lines:
            lines.append(f"[{text_line.confidence:.2f}] {text_line.text}")
        page_outputs.append(f"--- PAGE {page_idx + 1} ---\n" + "\n".join(lines))
        
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_doctr(pdf_path: str) -> str:
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
        
    return "\n\n--- PAGE BREAK ---\n\n".join(page_outputs)


def run_docling(pdf_path: str) -> str:
    from docling.document_converter import DocumentConverter
    converter = DocumentConverter()
    result = converter.convert(pdf_path)
    return result.document.export_to_markdown()


ENGINE_MAP = {
    "current": run_current_engine,
    "paddleocr": run_paddleocr,
    "surya": run_surya,
    "doctr": run_doctr,
    "docling": run_docling,
}


def main():
    parser = argparse.ArgumentParser(description="Single-file single-engine OCR benchmark runner.")
    parser.add_argument("--engine", required=True, choices=list(ENGINE_MAP.keys()), help="OCR engine to use")
    parser.add_argument("--file", required=True, help="Path or filename of target PDF")
    
    args = parser.parse_args()
    
    pdf_path = Path(args.file)
    if not pdf_path.exists():
        pdf_path = Path("documents") / args.file
    if not pdf_path.exists():
        print(f"Error: Could not find file {args.file} or documents/{args.file}", file=sys.stderr)
        sys.exit(1)
        
    out_dir_outputs = Path("tools/ocr_benchmark/outputs")
    out_dir_raw = Path("measurements/ocr_benchmark/raw")
    out_dir_outputs.mkdir(parents=True, exist_ok=True)
    out_dir_raw.mkdir(parents=True, exist_ok=True)
    
    out_filename = f"{pdf_path.stem}_{args.engine}.txt"
    out_file_outputs = out_dir_outputs / out_filename
    out_file_raw = out_dir_raw / out_filename
    
    print(f"=== Running OCR Benchmark ===")
    print(f"Engine: {args.engine}")
    print(f"File:   {pdf_path}")
    
    start_time = time.time()
    try:
        engine_fn = ENGINE_MAP[args.engine]
        text_output = engine_fn(str(pdf_path))
        elapsed = time.time() - start_time
        
        with open(out_file_outputs, "w", encoding="utf-8") as f:
            f.write(text_output)
        with open(out_file_raw, "w", encoding="utf-8") as f:
            f.write(text_output)
            
        print(f"Success! Elapsed time: {elapsed:.2f}s")
        print(f"Saved output to: {out_file_outputs}")
    except Exception as e:
        print(f"Execution failed for engine '{args.engine}': {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
