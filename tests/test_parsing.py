"""Unit tests for the deterministic number normalisation in src/extractor.py."""
import pytest

from src.extractor import apply_locale_decimal_parsing, parse_dot_decimal


@pytest.mark.parametrize("raw, expected", [
    ("1.628,16", "1628.16"),          # period thousands, comma decimal
    ("1.049.579,86", "1049579.86"),
    ("438,00", "438.00"),             # comma decimal only
    ("80 999 942.40", "80999942.40"),  # space thousands
    ("5,076.17", "5076.17"),          # comma thousands, period decimal
    ("28.031.70", "28031.70"),        # period as both thousands and decimal separator
    ("1234", "1234.00"),
    ("€ 12.50", "12.50"),
    ("-400,00", "-400.00"),
    ("(15.00)", "-15.00"),            # accounting-style negative
])
def test_parse_dot_decimal(raw, expected):
    assert parse_dot_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", None])
def test_parse_dot_decimal_empty_or_non_numeric(raw):
    assert parse_dot_decimal(raw) == ""


def test_apply_locale_decimal_parsing_normalises_all_levels():
    payable = {
        "gross_total": "1.234,50",
        "subtotal": "",
        "line_items": [{"quantity": "2", "unit_price": "617,25", "total": "1.234,50"}],
        "taxes": [{"tax_rate": "24", "tax_amount": "0,00"}],
    }
    out = apply_locale_decimal_parsing(payable)
    assert out["gross_total"] == "1234.50"
    assert out["subtotal"] == ""  # blanks stay blank
    assert out["line_items"][0] == {"quantity": "2.00", "unit_price": "617.25", "total": "1234.50"}
    assert out["taxes"][0] == {"tax_rate": "24.00", "tax_amount": "0.00"}
    assert payable["gross_total"] == "1.234,50"  # input not mutated
