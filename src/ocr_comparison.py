"""
Loads the OCR engine benchmark in measurements/ocr_benchmark/ for the app's comparison table.

Each engine folder holds one extracted text file per PDF and a run_info.txt with one line per
PDF ("PDF: X.pdf | Status: SUCCESS | Time: 1.23s"). Nothing is re-run: this only reads results.
"""
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

BENCHMARK_DIR = Path("measurements/ocr_benchmark")

# Display order and architecture of each engine, from docs/ocr_benchmark_and_project_roadmap.md.
ENGINES = {
    "current": "PyMuPDF text layer + EasyOCR (in production)",
    "paddle": "PaddleOCR 3.7 (PP-OCR detection + recognition)",
    "doctr": "docTR 1.1 (FAST + CRNN-VGG16)",
    "docling": "Docling (layout model + TableFormer + EasyOCR)",
}

# The harness's "current" engine reads the cached parsed_files/ text instead of running OCR,
# so its recorded time is a file read, not OCR latency.
CACHED_ENGINES = {"current"}

_RUN_LINE = re.compile(r"PDF:\s*(?P<pdf>\S+)\s*\|\s*Status:\s*(?P<status>\w+)\s*\|\s*Time:\s*(?P<secs>[\d.]+)s")
# PaddleOCR output prefixes each line with a confidence score, e.g. "[0.99] ".
_CONFIDENCE_PREFIX = re.compile(r"^\[[01]\.\d{2}\]\s*", re.MULTILINE)
# An amount as printed on an invoice: digits with exactly two decimals (407.95, 1.234,56).
_AMOUNT = re.compile(r"(?<![\d.,])\d[\d.,]*[.,]\d{2}(?![\d.,])")


@dataclass
class EngineRun:
    engine: str
    seconds: dict[str, float] = field(default_factory=dict)  # PDF stem -> seconds
    status: dict[str, str] = field(default_factory=dict)  # PDF stem -> status
    texts: dict[str, str] = field(default_factory=dict)  # PDF stem -> extracted text


def amounts_in(text: str) -> set[str]:
    """Distinct amount-like numbers in an engine's text, ignoring Paddle's confidence prefixes."""
    return set(_AMOUNT.findall(_CONFIDENCE_PREFIX.sub("", text)))


def load_runs(root: Path = BENCHMARK_DIR) -> list[EngineRun]:
    """One EngineRun per engine folder present under root, in ENGINES order."""
    runs = []
    for engine in ENGINES:
        folder = root / engine
        info = folder / "run_info.txt"
        if not info.exists():
            continue
        run = EngineRun(engine)
        for m in _RUN_LINE.finditer(info.read_text(encoding="utf-8")):
            stem = Path(m["pdf"]).stem
            run.status[stem] = m["status"]
            run.seconds[stem] = float(m["secs"])
            text_file = folder / f"{stem}.txt"
            run.texts[stem] = text_file.read_text(encoding="utf-8") if text_file.exists() else ""
        runs.append(run)
    return runs


def summary_rows(runs: list[EngineRun]) -> list[dict]:
    """One row per engine for the comparison table. Times are None for cached engines."""
    rows = []
    for run in runs:
        ok = sum(1 for s in run.status.values() if s == "SUCCESS")
        cached = run.engine in CACHED_ENGINES
        times = list(run.seconds.values())
        rows.append({
            "Engine": run.engine,
            "Method": ENGINES[run.engine],
            "PDFs completed": f"{ok}/{len(run.status)}",
            "Median time per PDF (s)": None if cached or not times else round(statistics.median(times), 1),
            "Slowest PDF (s)": None if cached or not times else round(max(times), 1),
            "Amounts read": sum(len(amounts_in(t)) for t in run.texts.values()),
        })
    return rows
