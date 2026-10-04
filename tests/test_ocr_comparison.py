"""Tests for the OCR benchmark loader behind the app's comparison table (src/ocr_comparison.py)."""
from src import ocr_comparison as oc


def _write_engine(root, engine, lines, texts):
    folder = root / engine
    folder.mkdir()
    (folder / "run_info.txt").write_text(
        f"Engine: {engine}\n" + "\n".join(lines) + "\nTotal Benchmark Execution Time: 1s\n", encoding="utf-8"
    )
    for stem, text in texts.items():
        (folder / f"{stem}.txt").write_text(text, encoding="utf-8")


def test_amounts_ignore_paddle_confidence_prefixes():
    text = "[0.99] Total 1.234,56\n[1.00] 407.95\nphone 6546 65 76\ndate 30.06.2026\n407.95"
    assert oc.amounts_in(text) == {"1.234,56", "407.95"}


def test_summary_reports_status_times_and_amounts(tmp_path):
    _write_engine(tmp_path, "doctr",
                  ["PDF: A.pdf | Status: SUCCESS | Time: 10.00s",
                   "PDF: B.pdf | Status: SUCCESS | Time: 30.00s",
                   "PDF: C.pdf | Status: FAILED | Time: 2.00s"],
                  {"A": "12.50 and 3.10", "B": "99.99"})
    _write_engine(tmp_path, "current", ["PDF: A.pdf | Status: SUCCESS | Time: 0.01s"], {"A": "12.50"})

    runs = oc.load_runs(tmp_path)
    assert [r.engine for r in runs] == ["current", "doctr"]  # ENGINES order, missing engines skipped
    current, doctr = oc.summary_rows(runs)

    assert doctr["PDFs completed"] == "2/3"
    assert doctr["Median time per PDF (s)"] == 10.0
    assert doctr["Slowest PDF (s)"] == 30.0
    assert doctr["Amounts read"] == 3
    # The harness reads current's cached text, so its time is not shown as OCR latency.
    assert current["Median time per PDF (s)"] is None


def test_committed_benchmark_loads():
    runs = oc.load_runs()
    assert [r.engine for r in runs] == list(oc.ENGINES)
    for run in runs:
        assert len(run.status) == 5 and all(run.texts.values())
