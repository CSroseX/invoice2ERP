"""Batch runner: regenerate output/<stem>.json for documents/*.pdf (issue #24).

    python -m src.batch [--reocr] [pdf ...]

Each PDF is processed from its cached OCR text in parsed_files/<stem>.txt when one exists,
so a rerun measures extraction changes rather than OCR drift; the PDF itself (which re-runs
OCR on scanned pages) is used only when there is no cache file or with --reocr.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from src.extractor import process_document_file
from src.logging_config import configure_console_logging

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = ROOT / "documents"
PARSED_DIR = ROOT / "parsed_files"
OUTPUT_DIR = ROOT / "output"

logger = logging.getLogger("invoice2erp.batch")


def text_source(pdf: Path, reocr: bool = False) -> Path:
    """The file to hand to process_document_file: the cached OCR text, else the PDF."""
    cached = PARSED_DIR / f"{pdf.stem}.txt"
    if not reocr and cached.is_file():
        return cached
    return pdf


def write_output(path: Path, result: dict) -> None:
    """Write the result JSON atomically, in the format of the committed output files."""
    data = json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.batch", description=__doc__.split("\n")[0])
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDFs to process (default: documents/*.pdf)")
    parser.add_argument("--reocr", action="store_true", help="run OCR on the PDF even if parsed_files/ has its text")
    args = parser.parse_args(argv)

    pdfs = args.pdfs or sorted(DOCUMENTS_DIR.glob("*.pdf"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    processed = payables = declined = failed = 0
    raised = []
    for pdf in pdfs:
        source = text_source(pdf, args.reocr)
        logger.info("processing %s (from %s)", pdf.name, source.suffix.lstrip("."))
        try:
            result = process_document_file(source)
            write_output(OUTPUT_DIR / f"{pdf.stem}.json", result)
        except Exception as e:
            # Error type only: provider error messages can echo document content.
            logger.error("%s raised %s; existing output left unchanged", pdf.name, type(e).__name__)
            raised.append(pdf.name)
            continue
        processed += 1
        payables += len(result.get("payables", []))
        declined += len(result.get("declined", []))
        failed += len(result.get("failed", []))

    print(
        f"{processed} documents processed: {payables} payables, {declined} declined, "
        f"{failed} failed; {len(raised)} documents raised"
        + (f" ({', '.join(raised)})" if raised else "")
    )
    return 1 if raised else 0


if __name__ == "__main__":
    configure_console_logging()
    sys.exit(main())
