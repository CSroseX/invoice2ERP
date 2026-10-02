"""Unit tests for src/grounding.py (Rule 1: every emitted value must appear on the document)."""
import pytest

from src.grounding import is_grounded_number, is_grounded_text, verify_payable_grounding

OCR = (
    "Invoice INV-2026-001\n"
    "Qty 4  Unit 12.50  Total 50.00\n"
    "Net 1 234,56\n"
    "Total amount: 1234.56\n"
)


@pytest.mark.parametrize("value", ["12.50", "50.00", "1234.56", "4", "4.00", ""])
def test_printed_numbers_are_grounded(value):
    assert is_grounded_number(value, OCR)


def test_comma_decimal_variant_is_grounded():
    assert is_grounded_number("438.00", "Summe 438,00 EUR")


@pytest.mark.parametrize("value", ["12.51", "99.99", "508.148"])
def test_unprinted_numbers_are_not_grounded(value):
    assert not is_grounded_number(value, OCR)


def test_fractional_value_does_not_ground_on_integer_prefix():
    # "508" is printed, but the derived "508.148" must not ground on it.
    assert not is_grounded_number("508.148", "Line total 508 EUR")


@pytest.mark.xfail(strict=True, reason="issue #3: normalised substring match crosses separators")
def test_digits_spread_across_unrelated_tokens_are_not_grounded():
    assert not is_grounded_number("1234.56", "Account 1234 56 ref")


@pytest.mark.xfail(strict=True, reason="issue #3: whole numbers match any digit run in the text")
def test_whole_number_does_not_ground_inside_another_number():
    assert not is_grounded_number("7.00", "Phone +372 555 7123")


def test_text_grounding():
    assert is_grounded_text("INV-2026-001", OCR)
    assert is_grounded_text("inv2026001", OCR)  # punctuation/case-insensitive
    assert not is_grounded_text("INV-9999", OCR)


def test_verify_payable_grounding_blanks_ungrounded_fields():
    payable = {
        "invoice_number": "INV-2026-001",
        "gross_total": "1234.56",
        "subtotal": "999.99",
        "line_items": [{"quantity": "4", "unit_price": "12.50", "total": "50.01"}],
        "taxes": [{"tax_rate": "24", "tax_amount": "77.77"}],
    }
    out, warnings = verify_payable_grounding(payable, OCR)

    assert out["invoice_number"] == "INV-2026-001"
    assert out["gross_total"] == "1234.56"
    assert out["subtotal"] == ""
    assert out["line_items"][0] == {"quantity": "4", "unit_price": "12.50", "total": ""}
    assert out["taxes"][0]["tax_amount"] == ""
    assert len(warnings) == 4
    assert payable["subtotal"] == "999.99"  # input not mutated


def test_declared_reconciliation_is_not_blanked():
    payable = {"line_items": [{"quantity": "4", "unit_price": "12.4999", "total": "50.00"}]}
    audit = {"line_reconciliations": [{"line_index": 0, "field": "unit_price"}]}

    out, warnings = verify_payable_grounding(payable, OCR, audit)

    assert out["line_items"][0]["unit_price"] == "12.4999"
    assert warnings == []
