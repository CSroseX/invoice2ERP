"""Offline tests for the review-queue logic behind the app's "Needs review" page (src/review.py)."""
import json
from datetime import datetime, timezone

import pytest

from src import review

BOOKS = {"gross_total": "10.00", "line_items": [{"quantity": "1", "unit_price": "10.00"}], "taxes": []}
MISMATCH = {"gross_total": "12.00", "line_items": [{"quantity": "1", "unit_price": "10.00"}], "taxes": []}


def _write_outputs(tmp_path, files):
    out = tmp_path / "output"
    out.mkdir()
    for stem, data in files.items():
        (out / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
    return out


def test_collects_all_three_categories(tmp_path):
    flagged = dict(BOOKS, __review__={"needed": True, "reasons": ["llm_response_truncated: repaired JSON after 4 providers"]})
    flagged_and_mismatch = dict(MISMATCH, __review__={"needed": True, "reasons": ["x"]})
    out = _write_outputs(tmp_path, {
        "A": {"file": "A.pdf", "payables": [BOOKS, flagged, MISMATCH], "declined": [], "failed": []},
        "B": {"file": "B.pdf", "payables": [flagged_and_mismatch], "declined": [],
              "failed": [{"segment": 2, "doc_type": "INVOICE", "error_type": "RuntimeError",
                          "reason": "Extraction failed (RuntimeError); see the processing log for details"}]},
        "C": {"file": "C.pdf", "payables": [BOOKS], "declined": [{"doc_type": "QUOTE", "reason": "quote"}]},
    })

    items = review.collect_review_items(out)

    assert [(i.file, i.kind, i.index, i.categories) for i in items] == [
        ("A.pdf", "payable", 1, ["flagged"]),
        ("A.pdf", "payable", 2, ["erp_mismatch"]),
        ("B.pdf", "segment", 2, ["failed"]),
        ("B.pdf", "payable", 0, ["flagged", "erp_mismatch"]),
    ]
    assert review.category_counts(items) == {"failed": 1, "flagged": 2, "erp_mismatch": 2}
    assert items[0].reasons == ["llm_response_truncated: repaired JSON after 4 providers"]
    assert "12.00" in items[1].reasons[0] and "10.00" in items[1].reasons[0]
    assert items[2].reasons == ["Extraction failed (RuntimeError); see the processing log for details"]


def test_tolerates_old_files_without_failed_and_bad_json(tmp_path):
    out = _write_outputs(tmp_path, {"OLD": {"file": "OLD.pdf", "payables": [BOOKS], "declined": []}})
    (out / "broken.json").write_text("{not json", encoding="utf-8")

    assert review.collect_review_items(out) == []


def test_review_flag_not_needed_is_ignored_and_file_name_falls_back_to_stem(tmp_path):
    out = _write_outputs(tmp_path, {"D": {"payables": [dict(BOOKS, __review__={"needed": False})]}})
    assert review.collect_review_items(out) == []

    out2 = tmp_path / "o2"
    out2.mkdir()
    (out2 / "E.json").write_text(json.dumps({"payables": [MISMATCH]}), encoding="utf-8")
    assert review.collect_review_items(out2)[0].file == "E.pdf"


@pytest.mark.parametrize("payable, passes", [
    (BOOKS, True),
    (dict(BOOKS, gross_total="10.04"), True),
    (dict(BOOKS, gross_total="10.05"), False),
    (dict(BOOKS, gross_total=""), False),
    ({"gross_total": "1.00", "line_items": ["not a dict"]}, False),
])
def test_erp_check(payable, passes):
    assert (review.erp_check(payable) is None) is passes


def test_decisions_are_appended_and_latest_wins(tmp_path):
    path = tmp_path / "review" / "decisions.jsonl"
    item = review.ReviewItem(file="A.pdf", kind="payable", index=1, categories=["flagged"], payable=BOOKS)
    other = review.ReviewItem(file="B.pdf", kind="segment", index=2, categories=["failed"])
    t1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

    review.record_decision(item, "Needs re-run", "check line 3", path=path, now=t1)
    review.record_decision(other, "Reject", path=path, now=t1)
    review.record_decision(item, "Approve", "  totals verified  ", path=path, now=t2)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    latest = review.latest_decisions(path)
    assert latest[item.key]["decision"] == "Approve"
    assert latest[item.key]["note"] == "totals verified"
    assert latest[item.key]["timestamp"] == "2026-10-02T09:00:00+00:00"
    assert latest[other.key]["decision"] == "Reject"
    # Metadata only: the payable's values are not written to the decisions file.
    assert "10.00" not in path.read_text(encoding="utf-8")


def test_invalid_decision_and_missing_file(tmp_path):
    item = review.ReviewItem(file="A.pdf", kind="payable", index=0)
    with pytest.raises(ValueError):
        review.record_decision(item, "Maybe", path=tmp_path / "d.jsonl")
    assert review.latest_decisions(tmp_path / "missing.jsonl") == {}


def test_edit_mode_requires_env_var():
    assert review.review_edit_enabled({review.EDIT_ENV_VAR: "1"})
    assert not review.review_edit_enabled({review.EDIT_ENV_VAR: "0"})
    assert not review.review_edit_enabled({})
