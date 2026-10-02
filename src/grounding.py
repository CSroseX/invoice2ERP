"""
grounding.py — Document Grounding & Anti-Hallucination Verifier.

Rule 1 Enforcement: "Every value you emit must appear on the document."
Verifies that every extracted field (invoice_number, dates, amounts, prices, quantities, PO numbers)
is traceable back to the raw OCR layout text. Ungrounded values are blanked out.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


def normalize_token(text: str) -> str:
    """Normalize text for fuzzy grounding comparison (lowercase, alphanumeric)."""
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()


def _number_variants(value: float) -> set[str]:
    """Spellings a printed amount can take: plain, comma-decimal, and grouped thousands."""
    v = abs(value)
    variants = set()
    exact = len(repr(v).partition(".")[2]) if v != int(v) else 0
    for decimals in {d for d in range(4) if round(v, d) == v} | {2, 3, exact}:
        dot = f"{v:.{decimals}f}"
        grouped = f"{v:,.{decimals}f}"  # 1,234.56
        variants |= {
            dot,
            dot.replace(".", ","),                                    # 1234,56
            grouped,                                                  # 1,234.56
            grouped.replace(",", "_").replace(".", ",").replace("_", "."),  # 1.234,56
            grouped.replace(",", " "),                                # 1 234.56
            grouped.replace(",", " ").replace(".", ","),              # 1 234,56
            grouped.replace(",", "'"),                                # 1'234.56 (CH)
            grouped.replace(",", "."),                                # 1.234.56 (OCR: dot as both)
        }
    if v == int(v):
        variants.add(str(int(v)))  # whole number printed without decimals, e.g. a quantity "4"
    return {x for x in variants if x}


@lru_cache(maxsize=8)
def _collapse_spaced_separators(text: str) -> str:
    """OCR often spaces a decimal separator ("83 , 21"); join it back ("83,21").

    Only an explicit "." or "," is joined. Digit groups with no separator between them
    ("53  40", "1234 56") stay apart, so they never ground a decimal value.
    """
    return re.sub(r"(?<=\d)[ \t]*([.,])[ \t]*(?=\d)", r"\1", text)


def _appears_as_number(token: str, text: str) -> bool:
    """True if `token` occurs in `text` as a whole number, not inside a longer digit run."""
    pattern = r"(?<![\d.,])" + re.escape(token) + r"(?![\d]|[.,]\d)"
    return re.search(pattern, text) is not None


def is_grounded_number(value_str: str, ocr_text: str) -> bool:
    """Check if a numeric value (e.g. '1234.56', '438.00', '15') is printed in the OCR text.

    The value must appear as a whole number in one of its printed spellings ("1234.56",
    "1234,56", "1,234.56", "1.234,56", "1 234,56", ...). It never matches inside a longer
    number or across separators: "7.00" is not grounded by "7123", and "1234.56" is not
    grounded by "1234 56". A fractional value must match its full fraction, so a derived
    "508.148" is not grounded by a printed "508". The sign is ignored, since documents print
    negatives as "-400,00", "400,00-" or "(400.00)".
    """
    if not value_str or not str(value_str).strip():
        return True  # Empty fields are valid (Rule 30)

    val_norm = str(value_str).strip()
    text = _collapse_spaced_separators(ocr_text)
    if _appears_as_number(val_norm.lstrip("-"), text):
        return True

    try:
        val_float = float(val_norm)
    except ValueError:
        return False

    return any(_appears_as_number(v, text) for v in _number_variants(val_float))


def is_grounded_text(value_str: str, ocr_text: str) -> bool:
    """Check if a text string (e.g. invoice_number, supplier_name) is grounded in OCR text."""
    if not value_str or not str(value_str).strip():
        return True

    clean_val = str(value_str).strip()
    if clean_val.lower() in ocr_text.lower():
        return True

    norm_val = normalize_token(clean_val)
    norm_ocr = normalize_token(ocr_text)

    if not norm_val:
        return True

    return norm_val in norm_ocr


def verify_payable_grounding(
    payable_dict: dict, ocr_text: str, reconciliation_audit: dict | None = None
) -> tuple[dict, list[str]]:
    """Audit a complete Autodraft Payable payload against OCR layout text.

    `reconciliation_audit` (from src.reconciler.reconcile_payable) names the fields that
    were deliberately derived rather than copied from the page — e.g. a unit_price rewritten
    because the document's own subtotal+tax==gross corroborated a rounding gap. Those fields
    are declared, not silently exempted: they are allowed to differ from the OCR text because
    the reconciler already recorded why, but every other field still must ground literally.

    Returns:
        (sanitized_payable_dict, list_of_ungrounded_warnings)
    """
    warnings = []
    sanitized = dict(payable_dict)

    reconciled_lines = {
        (rec["line_index"], rec["field"])
        for rec in (reconciliation_audit or {}).get("line_reconciliations", [])
    }

    # 1. Audit Header Fields. Amounts must ground as numbers; identifiers may also ground as text
    # (punctuation/case-insensitive), since "INV-001" may be printed as "INV 001".
    for field in ["invoice_number", "gross_total", "subtotal", "total_tax_amount", "po_number"]:
        val = str(sanitized.get(field, "") or "").strip()
        if field in ("invoice_number", "po_number"):
            grounded = is_grounded_number(val, ocr_text) or is_grounded_text(val, ocr_text)
        else:
            grounded = is_grounded_number(val, ocr_text)
        if val and not grounded:
            warnings.append(f"UNGROUNDED HEADER FIELD '{field}': '{val}' not found in OCR text. Blanking out field.")
            sanitized[field] = ""

    # 2. Audit Line Items
    if "line_items" in sanitized and isinstance(sanitized["line_items"], list):
        sanitized_lines = []
        for idx, line in enumerate(sanitized["line_items"]):
            san_line = dict(line)
            for l_field in ["quantity", "unit_price", "total", "tax_rate", "tax_amount"]:
                if (idx, l_field) in reconciled_lines:
                    continue  # declared reconciliation — see docstring
                l_val = str(san_line.get(l_field, "") or "").strip()
                if l_val and not is_grounded_number(l_val, ocr_text):
                    warnings.append(f"UNGROUNDED LINE ITEM [{idx}] '{l_field}': '{l_val}' not found in OCR text.")
                    san_line[l_field] = ""
            sanitized_lines.append(san_line)
        sanitized["line_items"] = sanitized_lines

    # 3. Audit Taxes
    if "taxes" in sanitized and isinstance(sanitized["taxes"], list):
        sanitized_taxes = []
        for idx, tax in enumerate(sanitized["taxes"]):
            san_tax = dict(tax)
            for t_field in ["tax_rate", "tax_amount"]:
                t_val = str(san_tax.get(t_field, "") or "").strip()
                if t_val and not is_grounded_number(t_val, ocr_text):
                    warnings.append(f"UNGROUNDED TAX [{idx}] '{t_field}': '{t_val}' not found in OCR text.")
                    san_tax[t_field] = ""
            sanitized_taxes.append(san_tax)
        sanitized["taxes"] = sanitized_taxes

    return sanitized, warnings
