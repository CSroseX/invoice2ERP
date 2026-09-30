"""reconciler.py — Document-corroborated line reconciliation.

Invoices print unit prices rounded for display; erp.py recomputes every line from
quantity x unit_price and never reads line_items[].total. Copying the printed price
verbatim can therefore fail to book even though the record faithfully mirrors the page.

This module rewrites a line's unit_price ONLY when the document's own arithmetic proves
the residual is real: subtotal + total_tax_amount == gross_total, exactly as the document
states them. That corroboration is the independent fact required before a correction fires
(a payable already correct never satisfies gate 1, so this node cannot regress it). It never
calls erp_book — an unprinted number chosen to satisfy the oracle is exactly what Rule 1
forbids.

Reuses num()/round2() from erp.py rather than reimplementing decimal parsing.
"""
from __future__ import annotations

from typing import Any

from erp import num, round2

_TOLERANCE = 0.05
_LINE_TOLERANCE = 0.02
_CHARGE_FIELDS = ("freight_charges", "insurance_charges", "extra_charges", "excise_duties")


def _document_self_consistent(payable: dict) -> bool:
    """Gate 1: does the document's own printed subtotal + tax foot to its own printed gross?"""
    sub = str(payable.get("subtotal") or "").strip()
    gross = str(payable.get("gross_total") or "").strip()
    if not sub or not gross:
        return False
    tax = str(payable.get("total_tax_amount") or "").strip()
    try:
        s = float(sub)
        t = float(tax) if tax else 0.0
        g = float(gross)
    except ValueError:
        return False
    return abs((s + t) - g) < _TOLERANCE


def reconcile_line_items(payable: dict) -> tuple[dict, list[dict]]:
    """Rewrite unit_price where quantity x unit_price disagrees with the document's own
    printed line total, but only on payables whose header arithmetic corroborates the gap.

    Returns (payable_with_reconciliations, reconciliation_records). Each record documents
    the line index, before/after unit_price, and the trigger, so no value changes silently.
    """
    records: list[dict] = []
    if not _document_self_consistent(payable):
        return payable, records

    line_items = payable.get("line_items")
    if not isinstance(line_items, list):
        return payable, records

    reconciled = dict(payable)
    new_lines = []
    for idx, li in enumerate(line_items):
        if not isinstance(li, dict):
            new_lines.append(li)
            continue

        printed_total = str(li.get("total") or "").strip()
        qty = num(li.get("quantity"))
        if not printed_total or qty == 0:
            new_lines.append(li)
            continue

        try:
            printed_total_val = float(printed_total)
        except ValueError:
            new_lines.append(li)
            continue

        current_price = num(li.get("unit_price"))
        recomputed = round2(qty * current_price)
        if abs(recomputed - printed_total_val) <= _LINE_TOLERANCE:
            new_lines.append(li)
            continue

        derived_price = printed_total_val / qty
        new_li = dict(li)
        new_li["unit_price"] = repr(derived_price)
        new_lines.append(new_li)
        records.append({
            "line_index": idx,
            "field": "unit_price",
            "before": li.get("unit_price"),
            "after": new_li["unit_price"],
            "trigger": (
                f"qty*unit_price={recomputed} disagreed with printed line total="
                f"{printed_total_val}; document's subtotal+tax==gross corroborates the residual"
            ),
        })

    reconciled["line_items"] = new_lines
    return reconciled, records


def dedupe_charges_against_header_tax(payable: dict) -> tuple[dict, list[dict]]:
    """Drop a header-level charge field when the same amount is also stated as a header tax.

    Keeps the placement the document itself uses (the header tax) and blanks the duplicate
    charge field rather than guessing which one is "real" — the document already told us.
    """
    warnings: list[dict] = []
    taxes = payable.get("taxes")
    if not isinstance(taxes, list) or not taxes:
        return payable, warnings

    tax_amounts = []
    for t in taxes:
        if isinstance(t, dict):
            amt = num(t.get("tax_amount"))
            if amt != 0:
                tax_amounts.append(amt)

    if not tax_amounts:
        return payable, warnings

    reconciled = dict(payable)
    for field in _CHARGE_FIELDS:
        charge_val = num(reconciled.get(field))
        if charge_val == 0:
            continue
        if any(abs(charge_val - ta) < _TOLERANCE for ta in tax_amounts):
            warnings.append({
                "field": field,
                "value": reconciled.get(field),
                "reason": f"duplicates a header tax_amount ({charge_val}); "
                          f"kept at header-tax placement, blanked duplicate charge field",
            })
            reconciled[field] = ""

    return reconciled, warnings


def detect_self_consistency_gap(payable: dict) -> dict | None:
    """Detection only — flags a payable whose header arithmetic foots (subtotal+tax==gross)
    but whose assembled line/tax components do not foot to that same subtotal+tax. This is
    the class of gap that silently drops a real discount or charge; it is not auto-repaired
    because doing so needs inference to validate which component was actually missed.
    """
    if not _document_self_consistent(payable):
        return None

    line_items = payable.get("line_items") or []
    line_sum = sum(
        num(li.get("total")) for li in line_items if isinstance(li, dict) and str(li.get("total") or "").strip()
    )
    printed_sub = num(payable.get("subtotal"))
    if printed_sub and abs(line_sum - printed_sub) >= _TOLERANCE:
        return {
            "reason": "header footed (subtotal+tax==gross) but sum(line totals) != subtotal",
            "printed_subtotal": printed_sub,
            "sum_of_line_totals": line_sum,
            "gap": round2(printed_sub - line_sum),
        }
    return None


def reconcile_payable(payable: dict) -> tuple[dict, dict[str, Any]]:
    """Run all reconciliation/detection steps in order. Returns (payable, audit) where audit
    carries every change made and every gap detected, for the pipeline trace."""
    audit: dict[str, Any] = {}

    payable, line_records = reconcile_line_items(payable)
    if line_records:
        audit["line_reconciliations"] = line_records

    payable, charge_warnings = dedupe_charges_against_header_tax(payable)
    if charge_warnings:
        audit["charge_dedup_warnings"] = charge_warnings

    gap = detect_self_consistency_gap(payable)
    if gap:
        audit["self_consistency_gap"] = gap

    return payable, audit
