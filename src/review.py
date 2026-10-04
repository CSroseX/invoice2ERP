"""review.py — what in output/*.json needs a human, and the decisions people made about it.

Used by the "Needs review" page of app.py. Three categories of review item:

- failed       a segment in a file's `failed` list (extraction raised an error)
- flagged      a payable whose `__review__.needed` is true (e.g. truncated LLM output)
- erp_mismatch a payable the ERP oracle does not book at its printed gross

One payable can be in both `flagged` and `erp_mismatch`; it is then a single item with
both categories. Human decisions are appended to a JSONL file (review/decisions.jsonl),
one record per decision; the latest record for an item is its current decision.
Decision records hold metadata and the reviewer's note only, never document values.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from erp import erp_book, num

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DECISIONS_PATH = ROOT / "review" / "decisions.jsonl"

# Decision controls are enabled only when this env var is "1"; the public showcase stays read-only.
EDIT_ENV_VAR = "INVOICE2ERP_REVIEW_EDIT"

CATEGORIES = {
    "failed": "Extraction failed",
    "flagged": "Flagged for review",
    "erp_mismatch": "ERP check failed",
}
DECISIONS = ("Approve", "Reject", "Needs re-run")

# Same tolerance as the ERP pass condition in CLAUDE.md and tools/measure_payables.py.
ERP_TOLERANCE = 0.05


@dataclass
class ReviewItem:
    file: str                      # source document name, e.g. "INV-01.pdf"
    kind: str                      # "segment" (a failed entry) or "payable"
    index: int                     # failed: the entry's segment number; payable: 0-based index in payables[]
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    payable: dict | None = None    # the payable (kind == "payable")
    entry: dict | None = None      # the failed entry (kind == "segment")

    @property
    def key(self) -> str:
        return item_key(self.file, self.kind, self.index)

    @property
    def label(self) -> str:
        where = f"segment {self.index}" if self.kind == "segment" else f"payable #{self.index + 1}"
        return f"{self.file} · {where}"


def item_key(file: str, kind: str, index: int) -> str:
    return f"{file}::{kind}::{index}"


def review_edit_enabled(environ: dict | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(EDIT_ENV_VAR, "").strip() == "1"


def erp_check(payable: dict) -> str | None:
    """None if the ERP oracle books the payable's printed gross, else a short reason."""
    if not str(payable.get("gross_total") or "").strip():
        return "No gross_total extracted, so the ERP check cannot pass"
    try:
        booked = erp_book(payable)["will_book_gross"]
    except ValueError as e:
        return f"ERP oracle rejected the payable: {e}"
    printed = num(payable.get("gross_total"))
    if abs(booked - printed) < ERP_TOLERANCE:
        return None
    return f"ERP books {booked:.2f} but the document's gross is {printed:.2f} (difference {booked - printed:+.2f})"


def load_outputs(output_dir: Path = OUTPUT_DIR) -> list[tuple[str, dict]]:
    """(source file name, output JSON) for every readable output/*.json, sorted by name."""
    results = []
    for path in sorted(Path(output_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            results.append((str(data.get("file") or f"{path.stem}.pdf"), data))
    return results


def items_for_output(file: str, data: dict) -> list[ReviewItem]:
    """Review items in one file's output. Tolerates files written before `failed` existed."""
    items = []
    for entry in data.get("failed") or []:
        if not isinstance(entry, dict):
            continue
        items.append(ReviewItem(
            file=file, kind="segment", index=int(entry.get("segment") or 0),
            categories=["failed"], reasons=[str(entry.get("reason") or "Extraction failed")], entry=entry,
        ))
    for idx, payable in enumerate(data.get("payables") or []):
        if not isinstance(payable, dict):
            continue
        item = ReviewItem(file=file, kind="payable", index=idx, payable=payable)
        review = payable.get("__review__")
        if isinstance(review, dict) and review.get("needed"):
            item.categories.append("flagged")
            item.reasons.extend(str(r) for r in (review.get("reasons") or ["Flagged for review"]))
        erp_reason = erp_check(payable)
        if erp_reason:
            item.categories.append("erp_mismatch")
            item.reasons.append(erp_reason)
        if item.categories:
            items.append(item)
    return items


def collect_review_items(output_dir: Path = OUTPUT_DIR) -> list[ReviewItem]:
    items = []
    for file, data in load_outputs(output_dir):
        items.extend(items_for_output(file, data))
    return items


def category_counts(items: list[ReviewItem]) -> dict[str, int]:
    return {cat: sum(1 for i in items if cat in i.categories) for cat in CATEGORIES}


def record_decision(
    item: ReviewItem, decision: str, note: str = "", path: Path = DECISIONS_PATH, now: datetime | None = None
) -> dict:
    """Append one decision record to the decisions file and return it."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    record = {
        "file": item.file,
        "kind": item.kind,
        "index": item.index,
        "categories": list(item.categories),
        "decision": decision,
        "note": note.strip(),
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def latest_decisions(path: Path = DECISIONS_PATH) -> dict[str, dict]:
    """The most recent decision per item key. Unreadable lines are skipped."""
    latest: dict[str, dict] = {}
    path = Path(path)
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            key = item_key(rec["file"], rec["kind"], int(rec["index"]))
        except (ValueError, KeyError, TypeError):
            continue
        latest[key] = rec  # append-only file: later lines win
    return latest
