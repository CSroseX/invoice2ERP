"""fallback.py — Regex-based extractor used when no LLM provider is available."""
from __future__ import annotations

import re

from src.extraction.postprocessing import parse_dot_decimal


def deterministic_extract_payable(ocr_text: str, filename: str = "") -> dict:
    """Deterministic spatial layout extractor (Fallback when API key is missing/quota exceeded)."""
    t = ocr_text

    inv_num = ""
    inv_match = re.search(r'(?:invoice|rechnung|arve|facture|fatura)\s*(?:nr|no|num|#|\.)*:?\s*([a-zA-Z0-9\-_]+)', t, re.IGNORECASE)
    if inv_match:
        inv_num = inv_match.group(1).strip()

    inv_date = ""
    date_match = re.search(r'(?:date|datum|kuup\u00e4ev)\s*:?\s*(\d{1,4}[\./\-]\d{1,2}[\./\-]\d{1,4})', t, re.IGNORECASE)
    if date_match:
        inv_date = date_match.group(1).strip()

    curr = "EUR"
    if "$" in t or "USD" in t:
        curr = "USD"
    elif "ZAR" in t or "R " in t:
        curr = "ZAR"
    elif "MYR" in t or "RM" in t:
        curr = "MYR"
    elif "GBP" in t or "£" in t:
        curr = "GBP"

    gross_total = ""
    gross_match = re.search(r'(?:endbetrag|gesamtsumme|total zar|total eur|total usd|amount due|grand total|total amount|total)\s*:?\s*([\$€£]?\s*[\d\.,]+)', t, re.IGNORECASE)
    if gross_match:
        gross_total = parse_dot_decimal(gross_match.group(1))

    tax_rate = ""
    tax_amt = ""
    tax_match = re.search(r'(?:vat|mwst|tax|sttax)\s*(?:at|@)?\s*(\d+[\.,]?\d*)\s*%\s*:?\s*([\$€£]?\s*[\d\.,]+)?', t, re.IGNORECASE)
    if tax_match:
        tax_rate = parse_dot_decimal(tax_match.group(1))
        if tax_match.group(2):
            tax_amt = parse_dot_decimal(tax_match.group(2))

    lines = [l.strip() for l in t.splitlines() if l.strip()]
    line_items = []
    for line in lines:
        row_match = re.search(r'^(.*?)\s+(\d+(?:[\.,]\d+)?)\s+(?:std|pcs|kg|hrs|hr|unit|ea)?\s*[\$€£]?\s*([\d\.,]+)\s+[\$€£]?\s*([\d\.,]+)$', line, re.IGNORECASE)
        if row_match:
            desc = row_match.group(1).strip()
            if not any(header_word in desc.lower() for header_word in ['beschreibung', 'description', 'subtotal', 'total']):
                line_items.append({
                    "description": desc,
                    "item_type": "SERVICE" if "std" in line.lower() or "hr" in line.lower() else "GOODS",
                    "uom": "Std" if "std" in line.lower() else "Pcs",
                    "quantity": parse_dot_decimal(row_match.group(2)),
                    "unit_price": parse_dot_decimal(row_match.group(3)),
                    "total": parse_dot_decimal(row_match.group(4)),
                    "discount": "",
                    "discount_percentage": "",
                    "tax_rate": "",
                    "tax_amount": "",
                    "taxes": []
                })

    taxes = []
    if tax_rate or tax_amt:
        taxes.append({
            "tax_type": "VAT",
            "tax_name": f"VAT {tax_rate}%" if tax_rate else "VAT",
            "tax_rate": tax_rate,
            "tax_amount": tax_amt,
            "tax_type_code": ""
        })

    payable = {
        "invoice_number": inv_num,
        "invoice_date": inv_date,
        "due_date": "",
        "invoice_type": "INVOICE",
        "currency": curr,
        "supplier": {
            "name": lines[0] if lines else "",
            "supplier_id": "",
            "address": lines[1] if len(lines) > 1 else "",
            "vat_id": ""
        },
        "buyer": {
            "company_code": "",
            "business_unit_code": "",
            "location_code": ""
        },
        "payment_term_id": "",
        "po_number": "",
        "po_id": "",
        "gross_total": gross_total,
        "subtotal": gross_total,
        "total_tax_amount": tax_amt,
        "discount_amount": "",
        "freight_charges": "",
        "insurance_charges": "",
        "extra_charges": "",
        "excise_duties": "",
        "taxes": taxes,
        "line_items": line_items
    }

    return payable
