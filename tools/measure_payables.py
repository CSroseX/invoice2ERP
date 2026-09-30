"""tools/measure_payables.py — READ-ONLY diagnostics.

For every payable in output/*.json (all 52) call erp_book() exactly as the
pipeline does and write measurements/payables_baseline.csv.

This file does NOT modify any pipeline, oracle, grounding, or extraction code.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# ── path setup so we can import erp from the project root ─────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from erp import erp_book, num  # noqa: E402  (project oracle – do not modify)

# ── output paths ──────────────────────────────────────────────────────────────
OUT_DIR = ROOT / "output"
CSV_PATH = ROOT / "measurements" / "payables_baseline.csv"
CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── tolerance constant (mirror of app.py line 267) ───────────────────────────
_TOLERANCE = 0.05  # abs(booked - printed) >= 0.05 → FAIL

# ── CSV columns ───────────────────────────────────────────────────────────────
COLUMNS = [
    "file",
    "payable_idx",
    "page_span_if_available",   # schema has no page-span field → always blank
    "doc_type",
    "currency",
    "status",
    "printed_gross",
    "printed_gross_empty_flag", # Y if gross_total is None/empty/unparseable
    "booked_gross",
    "diff",                     # booked_gross - printed_gross  (signed)
    "abs_diff",
    "n_line_items",
    "n_lines_empty_unit_price",
    "n_lines_empty_quantity",
    "n_lines_empty_line_total", # field name: "total" in line_items
    "n_lines_empty_tax",        # tax_rate AND tax_amount both empty/zero AND taxes list empty
    "printed_net_total",        # subtotal field if present
    "printed_tax_total",        # total_tax_amount field if present
    "sum_of_line_totals",       # sum of line_items[*].total as stored
    "sum_of_line_taxes",        # sum of line_items[*].tax_amount as stored
    "has_discount_or_rebate_field",  # Y/N based on schema keys
]


def _is_empty(v) -> bool:
    """True when a field value is absent, None, or blank string."""
    if v is None:
        return True
    return str(v).strip() == ""


def _parse_gross(raw) -> tuple[float, bool]:
    """Parse gross_total the same way app.py does (lines 262-266).
    Returns (float_value, is_empty_flag).
    """
    target_s = str(raw or "").strip()
    empty = target_s == ""
    try:
        val = float(target_s) if target_s else 0.0
    except ValueError:
        val = 0.0
        empty = True  # unparseable → treated as empty/0
    return val, empty


def _line_tax_empty(li: dict) -> bool:
    """True when a line item carries no usable tax information at all."""
    tax_rate_empty = _is_empty(li.get("tax_rate"))
    tax_amt_empty = _is_empty(li.get("tax_amount"))
    taxes_list_empty = not li.get("taxes")
    return tax_rate_empty and tax_amt_empty and taxes_list_empty


def _has_discount_field(p: dict) -> bool:
    """Y if the payable or any line item carries a discount / rebate key."""
    discount_keys = {
        "discount_amount", "discount_percentage", "discount",
        "rebate", "rebate_amount", "rebate_percentage",
    }
    if discount_keys & p.keys():
        return True
    for li in p.get("line_items") or []:
        if isinstance(li, dict) and (discount_keys & li.keys()):
            return True
    return False


def measure_payable(fname: str, idx: int, p: dict) -> dict:
    """Build one CSV row for payable p (index idx inside file fname)."""
    # ── erp_book call (identical to app.py line 260) ──────────────────────────
    erp_res = erp_book(p)
    booked_g: float = erp_res.get("will_book_gross", 0.0)

    # ── gross comparison (identical to app.py lines 262-268) ─────────────────
    printed_g, printed_empty = _parse_gross(p.get("gross_total"))
    status = "PASS" if abs(booked_g - printed_g) < _TOLERANCE else "FAIL"
    diff = booked_g - printed_g

    # ── line item metrics ─────────────────────────────────────────────────────
    line_items = p.get("line_items") or []
    n_li = len(line_items)
    n_empty_unit_price = sum(1 for li in line_items if isinstance(li, dict) and _is_empty(li.get("unit_price")))
    n_empty_qty        = sum(1 for li in line_items if isinstance(li, dict) and _is_empty(li.get("quantity")))
    n_empty_total      = sum(1 for li in line_items if isinstance(li, dict) and _is_empty(li.get("total")))
    n_empty_tax        = sum(1 for li in line_items if isinstance(li, dict) and _line_tax_empty(li))

    sum_line_totals = sum(num(li.get("total"))      for li in line_items if isinstance(li, dict))
    sum_line_taxes  = sum(num(li.get("tax_amount")) for li in line_items if isinstance(li, dict))

    # ── doc_type ──────────────────────────────────────────────────────────────
    doc_type = p.get("invoice_type", "")
    if _is_empty(doc_type):
        doc_type = "ABSENT"

    # ── currency ──────────────────────────────────────────────────────────────
    currency = p.get("currency", "")
    if _is_empty(currency):
        currency = "ABSENT"

    # ── printed net/tax totals ────────────────────────────────────────────────
    printed_net     = "" if "subtotal"          not in p else (p["subtotal"]         if not _is_empty(p["subtotal"])         else "")
    printed_tax_hdr = "" if "total_tax_amount"  not in p else (p["total_tax_amount"] if not _is_empty(p["total_tax_amount"]) else "")

    return {
        "file":                          fname,
        "payable_idx":                   idx,
        "page_span_if_available":        "",   # field absent in schema
        "doc_type":                      doc_type,
        "currency":                      currency,
        "status":                        status,
        "printed_gross":                 p.get("gross_total", ""),
        "printed_gross_empty_flag":      "Y" if printed_empty else "N",
        "booked_gross":                  booked_g,
        "diff":                          diff,
        "abs_diff":                      abs(diff),
        "n_line_items":                  n_li,
        "n_lines_empty_unit_price":      n_empty_unit_price,
        "n_lines_empty_quantity":        n_empty_qty,
        "n_lines_empty_line_total":      n_empty_total,
        "n_lines_empty_tax":             n_empty_tax,
        "printed_net_total":             printed_net,
        "printed_tax_total":             printed_tax_hdr,
        "sum_of_line_totals":            sum_line_totals,
        "sum_of_line_taxes":             sum_line_taxes,
        "has_discount_or_rebate_field":  "Y" if _has_discount_field(p) else "N",
    }


def main():
    json_files = sorted(f for f in OUT_DIR.glob("*.json") if f.is_file())
    rows: list[dict] = []

    for jf in json_files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        payables = data.get("payables") or []
        for idx, p in enumerate(payables):
            if not isinstance(p, dict):
                continue
            rows.append(measure_payable(jf.name, idx, p))

    # ── write CSV ─────────────────────────────────────────────────────────────
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    # ── summary ───────────────────────────────────────────────────────────────
    total = len(rows)
    n_pass = sum(1 for r in rows if r["status"] == "PASS")
    n_fail = sum(1 for r in rows if r["status"] == "FAIL")

    print(f"\n{'='*60}")
    print(f"CSV written to: {CSV_PATH}")
    print(f"{'='*60}")
    print(f"Total rows : {total}")
    print(f"PASS       : {n_pass}")
    print(f"FAIL       : {n_fail}")

    if total == 52 and n_pass == 27 and n_fail == 25:
        print("CHECK PASS: Counts match expected 52 / 27 / 25")
    else:
        print(f"DISCREPANCY - expected 52/27/25, got {total}/{n_pass}/{n_fail}")
        print("  Stopping; do NOT auto-fix - investigate manually.")
        return

    # ── per-file breakdown for DU-02.pdf ──────────────────────────────────────
    du02_rows = [r for r in rows if r["file"] == "DU-02.json"]
    du02_pass = sum(1 for r in du02_rows if r["status"] == "PASS")
    du02_fail = sum(1 for r in du02_rows if r["status"] == "FAIL")
    print(f"\nDU-02.pdf breakdown:")
    print(f"  total rows : {len(du02_rows)}")
    print(f"  PASS       : {du02_pass}")
    print(f"  FAIL       : {du02_fail}")
    for r in du02_rows:
        print(f"    idx={r['payable_idx']}  status={r['status']}  "
              f"printed_gross={r['printed_gross']!r}  "
              f"booked={r['booked_gross']:.2f}  diff={r['diff']:.4f}")

    # ── print full CSV contents ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("FULL CSV CONTENTS (52 rows):")
    print(f"{'='*60}")
    with CSV_PATH.open(encoding="utf-8") as fh:
        print(fh.read())

    # ── UNAVAILABLE FIELDS ────────────────────────────────────────────────────
    print(f"{'='*60}")
    print("UNAVAILABLE FIELDS (schema lacks these; columns kept blank in CSV):")
    print(f"{'='*60}")
    unavailable = [
        "page_span_if_available — no page-span / pages key exists in any payable",
    ]
    for u in unavailable:
        print(f"  - {u}")
    print()


if __name__ == "__main__":
    main()
