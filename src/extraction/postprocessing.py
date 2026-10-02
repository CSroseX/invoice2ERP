"""postprocessing.py — Deterministic business rules applied to the LLM's raw output.

Currency stripping, locale decimal normalisation, gross-total backfill, tax/charge
de-duplication, structural checks and JSON repair. No network access.
"""
from __future__ import annotations

import json
import re
from typing import Any


def num(v: Any) -> float:
    """Parse a plain dot-decimal number. Leading currency symbols and '%' are stripped."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").strip()
    s = re.sub(r'^[€$£¥₹\s]+', '', s).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def verify_structural_integrity(payable: dict) -> list[str]:
    """Perform structural audit on extracted payable payload."""
    warnings = []

    # 1. Tax Placement Audit
    has_header_taxes = bool(payable.get("taxes"))
    has_line_taxes = any(bool(item.get("taxes") or item.get("tax_rate")) for item in payable.get("line_items", []))
    if has_header_taxes and has_line_taxes:
        warnings.append("STRUCTURAL NOTICE: Document contains both header-level and line-level taxes.")

    # 2. Decomposed Components Audit
    for idx, item in enumerate(payable.get("line_items", [])):
        qty = item.get("quantity", "")
        price = item.get("unit_price", "")
        tot = item.get("total", "")
        if qty and price and tot:
            try:
                q_val = float(qty)
                p_val = float(price)
                t_val = float(tot)
                calc_tot = q_val * p_val
                if abs(calc_tot - t_val) > 0.05 and not item.get("discount") and not item.get("discount_percentage"):
                    warnings.append(f"LINE [{idx}] DECOMPOSITION NOTICE: qty ({qty}) * unit_price ({price}) = {calc_tot:.2f} != total ({tot}). Possible discount/charge included.")
            except ValueError:
                pass

    return warnings


ISO_CURRENCY_CODES = {
    "EUR", "USD", "ZAR", "KES", "GHC", "GHS", "TRY", "GBP", "MYR", "CAD", "AUD", 
    "CHF", "INR", "JPY", "NZD", "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "BRL", 
    "MXN", "EGP", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "PKR", "LKR", "BDT", 
    "THB", "IDR", "PHP", "VND", "TWD", "KRW", "SGD", "HKD", "CNY", "ILS", "RON", "BGN"
}

CURRENCY_SYMBOLS_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "R": "ZAR",
    "RM": "MYR",
}


def strip_currency_token(val_str: str) -> tuple[str, str]:
    """FIX 1: Pre-processing step to strip leading/trailing currency tokens (ISO codes & symbols).
    Returns (stripped_numeric_string, captured_currency_code).
    """
    if not val_str or not isinstance(val_str, str):
        return "", ""

    s = val_str.strip()
    if not s:
        return "", ""

    captured_curr = ""

    # Leading currency token (3-letter ISO code or currency symbol)
    m_lead = re.match(r'^(?:([A-Za-z]{3})|([\$€£¥₹R]))\s*(.*)$', s)
    if m_lead:
        iso, sym, rest = m_lead.groups()
        if rest and re.search(r'\d', rest):
            if iso and iso.upper() in ISO_CURRENCY_CODES:
                s = rest.strip()
                captured_curr = iso.upper()
            elif sym and sym in CURRENCY_SYMBOLS_MAP:
                s = rest.strip()
                captured_curr = CURRENCY_SYMBOLS_MAP.get(sym, sym)

    # Trailing currency token
    m_trail = re.search(r'^(.*?)\s*(?:([A-Za-z]{3})|([\$€£¥₹R]))$', s)
    if m_trail:
        rest, iso, sym = m_trail.groups()
        if rest and re.search(r'\d', rest):
            if iso and iso.upper() in ISO_CURRENCY_CODES:
                s = rest.strip()
                if not captured_curr:
                    captured_curr = iso.upper()
            elif sym and sym in CURRENCY_SYMBOLS_MAP:
                s = rest.strip()
                if not captured_curr:
                    captured_curr = CURRENCY_SYMBOLS_MAP.get(sym, sym)

    return s, captured_curr


def apply_currency_stripping(payable: dict) -> dict:
    """FIX 1: Pre-processing step to strip leading/trailing currency tokens
    from all numeric fields in a payable dict without losing captured currency.
    """
    cleaned = dict(payable)
    extracted_curr = ""

    header_fields = ["gross_total", "subtotal", "total_tax_amount", "discount_amount", 
                     "freight_charges", "insurance_charges", "extra_charges", "excise_duties"]
    for field in header_fields:
        val = str(cleaned.get(field) or "").strip()
        if val:
            stripped, curr = strip_currency_token(val)
            cleaned[field] = stripped
            if curr and not extracted_curr:
                extracted_curr = curr

    if "line_items" in cleaned and isinstance(cleaned["line_items"], list):
        san_lines = []
        for item in cleaned["line_items"]:
            li = dict(item)
            for l_field in ["quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount"]:
                l_val = str(li.get(l_field) or "").strip()
                if l_val:
                    stripped, curr = strip_currency_token(l_val)
                    li[l_field] = stripped
                    if curr and not extracted_curr:
                        extracted_curr = curr
            san_lines.append(li)
        cleaned["line_items"] = san_lines

    if "taxes" in cleaned and isinstance(cleaned["taxes"], list):
        san_taxes = []
        for t in cleaned["taxes"]:
            tax_item = dict(t)
            for t_field in ["tax_rate", "tax_amount"]:
                t_val = str(tax_item.get(t_field) or "").strip()
                if t_val:
                    stripped, curr = strip_currency_token(t_val)
                    tax_item[t_field] = stripped
                    if curr and not extracted_curr:
                        extracted_curr = curr
            san_taxes.append(tax_item)
        cleaned["taxes"] = san_taxes

    if extracted_curr and not cleaned.get("currency"):
        cleaned["currency"] = extracted_curr

    return cleaned


def parse_dot_decimal(val_str: str) -> str:
    """FIX 2: Clean and convert any numeric string to standard dot-decimal ('1628.16').
    Handles:
      (a) period-as-thousands + comma-as-decimal ("1.628,16" -> "1628.16")
      (b) space-as-thousands ("80 999 942.40" -> "80999942.40")
      (c) comma-as-thousands + period-as-decimal ("5,076.17" -> "5076.17")
    """
    if not val_str or not isinstance(val_str, str):
        return ""
    
    s = val_str.strip()
    if not s:
        return ""
    
    is_negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    
    cleaned = re.sub(r'[^0-9\., ]', '', s).strip()
    if not cleaned:
        return ""
    
    # Pattern (b): space-as-thousands -> remove spaces between digit groups
    if re.search(r'\d\s+\d', cleaned):
        cleaned = re.sub(r'(?<=\d)\s+(?=\d)', '', cleaned)
    
    last_comma = cleaned.rfind(',')
    last_period = cleaned.rfind('.')
    
    if last_comma != -1 and last_comma > last_period:
        # Pattern (a): Comma is decimal separator (e.g. "1.628,16" or "1.049.579,86" or "438,00")
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif last_period != -1 and last_period > last_comma:
        # Pattern (c): Period is decimal separator (e.g. "5,076.17" or "80999942.40")
        cleaned = cleaned.replace(',', '')
    elif last_comma != -1 and last_period == -1:
        # Only comma present (e.g. "438,00")
        cleaned = cleaned.replace(',', '.')
    elif last_period != -1 and last_comma == -1:
        # Only period present
        pass

    # Period used as BOTH thousands and decimal separator ("28.031.70" -> "28031.70").
    # Without this, float() raises and the unparseable string is returned verbatim,
    # which erp.num() later reads as 0.0.
    if cleaned.count('.') > 1:
        split_at = cleaned.rfind('.')
        cleaned = cleaned[:split_at].replace('.', '') + '.' + cleaned[split_at + 1:]

    try:
        f = float(cleaned)
        if is_negative:
            f = -abs(f)
        return f"{f:.2f}"
    except ValueError:
        return cleaned


def apply_locale_decimal_parsing(payable: dict) -> dict:
    """FIX 2: Pre-processing step to normalize all numeric fields in a payable dict to dot-decimal."""
    cleaned = dict(payable)

    header_fields = ["gross_total", "subtotal", "total_tax_amount", "discount_amount", 
                     "freight_charges", "insurance_charges", "extra_charges", "excise_duties"]
    for field in header_fields:
        val = str(cleaned.get(field) or "").strip()
        if val:
            cleaned[field] = parse_dot_decimal(val)

    if "line_items" in cleaned and isinstance(cleaned["line_items"], list):
        san_lines = []
        for item in cleaned["line_items"]:
            li = dict(item)
            for l_field in ["quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount"]:
                l_val = str(li.get(l_field) or "").strip()
                if l_val:
                    li[l_field] = parse_dot_decimal(l_val)
            san_lines.append(li)
        cleaned["line_items"] = san_lines

    if "taxes" in cleaned and isinstance(cleaned["taxes"], list):
        san_taxes = []
        for t in cleaned["taxes"]:
            tax_item = dict(t)
            for t_field in ["tax_rate", "tax_amount"]:
                t_val = str(tax_item.get(t_field) or "").strip()
                if t_val:
                    tax_item[t_field] = parse_dot_decimal(t_val)
            san_taxes.append(tax_item)
        cleaned["taxes"] = san_taxes

    return cleaned


def apply_fix3_and_fix4_postprocessing(payable: dict, ocr_text: str = "") -> dict:
    """FIX 3 & FIX 4 post-processing sanitization:
    - FIX 3: Ensure gross_total is extracted if present in OCR text under alternate labels.
    - FIX 4: Clean quantity fractions ('1/1' -> '1') and remove duplicated line tax_amounts.
    """
    cleaned = dict(payable)
    
    # FIX 3: If gross_total is blank, search OCR text for total keywords
    if not str(cleaned.get("gross_total") or "").strip() and ocr_text:
        gross_match = re.search(
            r'(?:endbetrag|gesamtsumme|total zar|total eur|total usd|amount due|grand total|total amount|total due|net current reimbursement|balance due|amount payable|total)\s*:?\s*([\$€£]?\s*[\d\.,]+)',
            ocr_text, re.IGNORECASE
        )
        if gross_match:
            raw_gt, _ = strip_currency_token(gross_match.group(1))
            cleaned["gross_total"] = parse_dot_decimal(raw_gt)

    # FIX 4: Clean line items (quantity fractions & duplicated line tax_amount)
    if "line_items" in cleaned and isinstance(cleaned["line_items"], list):
        san_lines = []
        for item in cleaned["line_items"]:
            li = dict(item)
            
            # 1. Clean quantity fraction (X/Y -> Y)
            qty = str(li.get("quantity") or "").strip()
            m_qty = re.match(r'^\d+\s*/\s*(\d+(?:\.\d+)?)$', qty)
            if m_qty:
                li["quantity"] = m_qty.group(1)
                
            # 2. Sanitize duplicated line tax_amount
            tax_amt = str(li.get("tax_amount") or "").strip()
            tot_amt = str(li.get("total") or "").strip()
            tax_rate = str(li.get("tax_rate") or "").strip()
            taxes = li.get("taxes") or []
            
            if tax_amt and tot_amt and tax_amt == tot_amt and not tax_rate and not taxes:
                li["tax_amount"] = ""
                
            # 3. Backfill unit_price if missing but quantity and total exist
            u_price = str(li.get("unit_price") or "").strip()
            tot = str(li.get("total") or "").strip()
            if not u_price and li.get("quantity") and tot:
                try:
                    q_val = float(li["quantity"])
                    t_val = float(tot)
                    if q_val != 0:
                        li["unit_price"] = f"{(t_val / q_val):.2f}"
                except ValueError:
                    pass

            san_lines.append(li)
        cleaned["line_items"] = san_lines

    return cleaned


def deduplicate_tax_placement(payable: dict) -> dict:
    """General Rule 5 & Rule 11 Tax Placement & Charge Deduplication Logic:
    Prevents tax and fee duplication between line_items[] and header fields.
    """
    cleaned = dict(payable)
    header_taxes = cleaned.get("taxes") or []
    line_items = cleaned.get("line_items") or []

    gt = num(cleaned.get("gross_total"))
    freight = num(cleaned.get("freight_charges"))
    extra = num(cleaned.get("extra_charges"))
    header_tax_sum = sum(num(t.get("tax_amount")) for t in header_taxes if isinstance(t, dict))

    # 1. Withholding tax sign fix (withholding tax reduces gross payable)
    if header_taxes:
        san_taxes = []
        for t in header_taxes:
            if isinstance(t, dict):
                t_copy = dict(t)
                ttype = str(t_copy.get("tax_type") or "").upper()
                tname = str(t_copy.get("tax_name") or "").upper()
                if "WITHHOLDING" in ttype or "WITHHOLDING" in tname or "WHT" in ttype or "WHT" in tname:
                    amt = num(t_copy.get("tax_amount"))
                    if amt > 0:
                        t_copy["tax_amount"] = f"-{amt:.2f}"
                    rate = num(t_copy.get("tax_rate"))
                    if rate > 0:
                        t_copy["tax_rate"] = f"-{rate:.2f}"
                san_taxes.append(t_copy)
            else:
                san_taxes.append(t)
        cleaned["taxes"] = san_taxes
        header_taxes = cleaned["taxes"]

    # 2. Freight / Charge deduplication (if freight charge is already a line item)
    if freight > 0 and line_items:
        for li in line_items:
            desc = str(li.get("description") or "").lower()
            l_tot = num(li.get("total"))
            if any(k in desc for k in ["freight", "surcharge", "shipping", "delivery", "transport"]) and abs(l_tot - freight) < 0.05:
                cleaned["freight_charges"] = ""
                break

    # 3. Tax on gross line items deduplication
    if header_taxes and line_items and gt > 0:
        line_tot_sum = sum(num(li.get("total")) for li in line_items)
        if abs(line_tot_sum - gt) < 0.05:
            cleaned["taxes"] = []
            header_taxes = []

    has_header_tax = any(
        str(t.get("tax_amount") or "").strip() or str(t.get("tax_rate") or "").strip()
        for t in header_taxes if isinstance(t, dict)
    )

    if not has_header_tax or not line_items:
        return cleaned

    line_rates = set()
    for li in line_items:
        if isinstance(li, dict):
            r = str(li.get("tax_rate") or "").replace("%", "").strip()
            try:
                f_r = float(r)
                line_rates.add(f"{f_r:.2f}")
            except ValueError:
                if r:
                    line_rates.add(r.upper())

    if len(line_rates) > 1:
        cleaned["taxes"] = []
        return cleaned

    san_lines = []
    for li in line_items:
        if isinstance(li, dict):
            item_c = dict(li)
            item_c["tax_rate"] = ""
            item_c["tax_amount"] = ""
            item_c["taxes"] = []
            san_lines.append(item_c)
        else:
            san_lines.append(li)
    cleaned["line_items"] = san_lines

    return cleaned


def repair_json_string(s: str) -> str:
    """Repair common LLM JSON syntax glitches (unterminated strings, missing closing braces/brackets, trailing commas)."""
    if not s or not isinstance(s, str):
        return "{}"
    s = s.strip()
    if "```json" in s:
        s = s.split("```json")[1].split("```")[0].strip()
    elif "```" in s:
        s = s.split("```")[1].split("```")[0].strip()
    
    start_idx = s.find("{")
    if start_idx != -1:
        s = s[start_idx:]
    
    s = re.sub(r',\s*([\}\]])', r'\1', s)
    
    try:
        json.loads(s, strict=False)
        return s
    except Exception:
        pass
        
    s_clean = re.sub(r'(?<!\\)[\r\n]+', ' ', s)
    try:
        json.loads(s_clean, strict=False)
        return s_clean
    except Exception:
        pass

    quote_count = len(re.findall(r'(?<!\\)"', s_clean))
    if quote_count % 2 != 0:
        s_clean += '"'
        
    open_braces = s_clean.count('{') - s_clean.count('}')
    open_brackets = s_clean.count('[') - s_clean.count(']')
    
    s_clean += ']' * max(0, open_brackets)
    s_clean += '}' * max(0, open_braces)
    
    return s_clean
