"""Unit tests for src/reconciler.py (document-corroborated line reconciliation)."""
import pytest

from src.reconciler import (
    _format_price,
    dedupe_charges_against_header_tax,
    detect_self_consistency_gap,
    reconcile_line_items,
    reconcile_payable,
)


def _payable(**overrides):
    # Header foots: 100.00 + 24.00 == 124.00. Line prints a rounded unit price (7 x 14.29 = 100.03).
    base = {
        "subtotal": "100.00",
        "total_tax_amount": "24.00",
        "gross_total": "124.00",
        "line_items": [{"quantity": "7", "unit_price": "14.29", "total": "100.00"}],
        "taxes": [],
    }
    base.update(overrides)
    return base


def test_rewrites_unit_price_when_header_corroborates():
    out, records = reconcile_line_items(_payable())

    assert abs(7 * float(out["line_items"][0]["unit_price"]) - 100.00) < 0.005  # books to the cent
    assert len(records) == 1
    assert records[0]["line_index"] == 0
    assert records[0]["before"] == "14.29"


def test_no_rewrite_when_header_does_not_foot():
    payable = _payable(gross_total="130.00")
    out, records = reconcile_line_items(payable)

    assert records == []
    assert out["line_items"][0]["unit_price"] == "14.29"


def test_no_rewrite_when_line_already_matches():
    payable = _payable(line_items=[{"quantity": "4", "unit_price": "25.00", "total": "100.00"}])
    _, records = reconcile_line_items(payable)
    assert records == []


def test_no_rewrite_without_printed_total_or_quantity():
    payable = _payable(line_items=[
        {"quantity": "7", "unit_price": "14.29", "total": ""},
        {"quantity": "", "unit_price": "14.29", "total": "100.00"},
    ])
    _, records = reconcile_line_items(payable)
    assert records == []


def test_reconcile_does_not_mutate_input():
    payable = _payable()
    reconcile_line_items(payable)
    assert payable["line_items"][0]["unit_price"] == "14.29"


def test_charge_equal_to_header_tax_is_blanked():
    payable = _payable(taxes=[{"tax_amount": "24.00"}], excise_duties="24.00", freight_charges="10.00")
    out, warnings = dedupe_charges_against_header_tax(payable)

    assert out["excise_duties"] == ""
    assert out["freight_charges"] == "10.00"
    assert [w["field"] for w in warnings] == ["excise_duties"]


def test_self_consistency_gap_detected():
    payable = _payable(line_items=[{"quantity": "1", "unit_price": "90.00", "total": "90.00"}])
    gap = detect_self_consistency_gap(payable)
    assert gap is not None
    assert gap["gap"] == 10.00


def test_reconcile_payable_audit_lists_changes():
    _, audit = reconcile_payable(_payable())
    assert "line_reconciliations" in audit
    assert "self_consistency_gap" not in audit


def test_derived_unit_price_rounded_to_six_decimals():
    out, _ = reconcile_line_items(_payable())
    assert out["line_items"][0]["unit_price"] == "14.285714"  # 100 / 7


@pytest.mark.parametrize("value, expected", [(1.6325, "1.6325"), (5.0, "5.00"), (2743.44, "2743.44"),
                                             (100 / 3, "33.333333")])
def test_format_price(value, expected):
    assert _format_price(value) == expected
