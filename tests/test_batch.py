"""Offline tests for the batch runner (issue #24): process_document_file is stubbed."""
import json

import pytest

import src.batch as batch


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    docs, parsed, out = tmp_path / "documents", tmp_path / "parsed_files", tmp_path / "output"
    for d in (docs, parsed, out):
        d.mkdir()
    monkeypatch.setattr(batch, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(batch, "PARSED_DIR", parsed)
    monkeypatch.setattr(batch, "OUTPUT_DIR", out)
    return docs, parsed, out


def _stub(monkeypatch, raise_for=()):
    """Record the paths process_document_file receives; raise for the given stems."""
    calls = []

    def fake(path):
        calls.append(path)
        if path.stem in raise_for:
            raise RuntimeError("LLM API Call Failed: echoed IBAN DE89370400440532013000")
        return {"file": f"{path.stem}.pdf", "payables": [{"invoice_number": "Ü-1"}], "declined": [], "failed": []}

    monkeypatch.setattr(batch, "process_document_file", fake)
    return calls


def test_cached_text_is_preferred_over_the_pdf(dirs, monkeypatch):
    docs, parsed, _ = dirs
    (docs / "A.pdf").write_bytes(b"%PDF")
    (docs / "B.pdf").write_bytes(b"%PDF")
    (parsed / "A.txt").write_text("INVOICE", encoding="utf-8")
    calls = _stub(monkeypatch)

    assert batch.main([]) == 0
    assert calls == [parsed / "A.txt", docs / "B.pdf"]


def test_reocr_passes_the_pdf(dirs, monkeypatch):
    docs, parsed, _ = dirs
    (docs / "A.pdf").write_bytes(b"%PDF")
    (parsed / "A.txt").write_text("INVOICE", encoding="utf-8")
    calls = _stub(monkeypatch)

    assert batch.main(["--reocr"]) == 0
    assert calls == [docs / "A.pdf"]


def test_explicit_paths_limit_the_run(dirs, monkeypatch):
    docs, _, _ = dirs
    for stem in ("A", "B"):
        (docs / f"{stem}.pdf").write_bytes(b"%PDF")
    calls = _stub(monkeypatch)

    assert batch.main([str(docs / "B.pdf")]) == 0
    assert [p.name for p in calls] == ["B.pdf"]


def test_output_format_matches_the_committed_files(dirs, monkeypatch):
    docs, _, out = dirs
    (docs / "A.pdf").write_bytes(b"%PDF")
    _stub(monkeypatch)

    batch.main([])
    raw = (out / "A.json").read_bytes()
    result = {"file": "A.pdf", "payables": [{"invoice_number": "Ü-1"}], "declined": [], "failed": []}
    assert raw == json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
    assert b"\r" not in raw and not raw.endswith(b"\n")
    assert list(out.iterdir()) == [out / "A.json"]


def test_a_raising_document_does_not_stop_the_others(dirs, monkeypatch, capsys, caplog):
    docs, _, out = dirs
    for stem in ("A", "B", "C"):
        (docs / f"{stem}.pdf").write_bytes(b"%PDF")
    (out / "B.json").write_text("old output", encoding="utf-8")
    calls = _stub(monkeypatch, raise_for={"B"})

    assert batch.main([]) == 1
    assert [p.stem for p in calls] == ["A", "B", "C"]
    assert (out / "B.json").read_text(encoding="utf-8") == "old output"
    assert (out / "A.json").exists() and (out / "C.json").exists()
    assert "2 documents processed" in capsys.readouterr().out
    assert "RuntimeError" in caplog.text and "DE89" not in caplog.text


def test_a_failed_write_leaves_no_partial_file(dirs, monkeypatch):
    docs, _, out = dirs
    (docs / "A.pdf").write_bytes(b"%PDF")
    (out / "A.json").write_text("old output", encoding="utf-8")
    _stub(monkeypatch)

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(batch.os, "replace", broken_replace)
    assert batch.main([]) == 1
    assert list(out.iterdir()) == [out / "A.json"]
    assert (out / "A.json").read_text(encoding="utf-8") == "old output"


def test_summary_counts(dirs, monkeypatch, capsys):
    docs, _, _ = dirs
    (docs / "A.pdf").write_bytes(b"%PDF")

    def fake(path):
        return {"file": "A.pdf", "payables": [{}, {}], "declined": [{}],
                "failed": [{"error_type": "ValueError"}]}

    monkeypatch.setattr(batch, "process_document_file", fake)
    assert batch.main([]) == 0
    assert capsys.readouterr().out.strip() == (
        "1 documents processed: 2 payables, 1 declined, 1 failed; 0 documents raised"
    )
