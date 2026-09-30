"""Read-only diagnostic: print exact erp_book inputs and internals for selected payables."""
import json
import sys
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

ROOT = Path(r"c:\Users\chitr\Desktop\coding\Zycus Assignment\candidate_kit")
sys.path.insert(0, str(ROOT))
from erp import erp_book, num, _line_base, _line_taxes, _header_taxes

TARGETS = {
    "INV-07.json": [0],
    "HLD-01.json": [0],
    "HLD-05.json": [0],
    "INV-21.json": [0],
    "DU-03.json":  [1],
}

OUT = ROOT / "output"

STANDARD_NON_AMOUNT = {
    "invoice_number", "invoice_date", "due_date", "invoice_type",
    "currency", "supplier", "buyer", "payment_term_id", "po_number",
    "po_id", "taxes", "line_items",
}
AMT_FIELDS = [
    "gross_total", "subtotal", "total_tax_amount", "discount_amount",
    "freight_charges", "insurance_charges", "extra_charges", "excise_duties",
]


def round2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def diagnose(label: str, p: dict) -> None:
    print("=" * 70)
    print(f"PAYABLE: {label}")
    print("=" * 70)

    print()
    print("--- AMOUNT-RELATED FIELDS (raw, as stored) ---")
    for k in AMT_FIELDS:
        val = p.get(k, "<KEY ABSENT>")
        print(f"  {k}: {val!r}")
    extra_keys = {k: p[k] for k in p if k not in AMT_FIELDS and k not in STANDARD_NON_AMOUNT}
    if extra_keys:
        print("  (other non-standard top-level fields:)")
        for k, v in extra_keys.items():
            print(f"    {k}: {v!r}")

    print()
    print("--- HEADER taxes[] (raw) ---")
    hdr_taxes = p.get("taxes") or []
    if hdr_taxes:
        for t in hdr_taxes:
            print(f"  {json.dumps(t)}")
    else:
        print("  (empty list)")

    print()
    print("--- ERP_BOOK INTERNAL COMPONENTS ---")
    line_items = p.get("line_items") or []
    item_disc_total = 0.0
    line_tax_total = 0.0
    print(f"  Line items ({len(line_items)} total):")
    for i, li in enumerate(line_items):
        base = _line_base(li)
        lt = _line_taxes(li, base)
        item_disc_total += base
        line_tax_total += lt
        qty   = li.get("quantity")
        price = li.get("unit_price")
        tot   = li.get("total")
        disc  = li.get("discount")
        dpct  = li.get("discount_percentage")
        tr    = li.get("tax_rate")
        ta    = li.get("tax_amount")
        taxes = li.get("taxes")
        print(f"    [{i}] qty={qty!r} unit_price={price!r} total={tot!r} "
              f"discount={disc!r} disc_pct={dpct!r} "
              f"tax_rate={tr!r} tax_amount={ta!r} taxes={taxes}"
              f"  => _line_base={base}  _line_taxes={lt}")

    header_discount = abs(num(p.get("discount_amount")))
    net_base = item_disc_total - header_discount
    header_tax = _header_taxes(hdr_taxes, net_base)
    freight   = num(p.get("freight_charges"))
    insurance = num(p.get("insurance_charges"))
    extra     = num(p.get("extra_charges"))
    excise    = num(p.get("excise_duties"))
    other_charges = freight + insurance + extra + excise

    pre_round = item_disc_total - header_discount + line_tax_total + header_tax + other_charges
    will_book = round2(pre_round)

    print()
    print(f"  item_discounted_total  = {item_disc_total}")
    print(f"  line_tax_total         = {line_tax_total}")
    print(f"  header_discount (abs)  = {header_discount}  (raw discount_amount={p.get('discount_amount')!r})")
    print(f"  net_base               = {net_base}")
    print(f"  header_tax             = {header_tax}  (computed from taxes[] on net_base)")
    print(f"  freight_charges (num)  = {freight}  (raw={p.get('freight_charges')!r})")
    print(f"  insurance_charges(num) = {insurance}  (raw={p.get('insurance_charges')!r})")
    print(f"  extra_charges (num)    = {extra}  (raw={p.get('extra_charges')!r})")
    print(f"  excise_duties (num)    = {excise}  (raw={p.get('excise_duties')!r})")
    print(f"  other_charges          = {other_charges}")
    print()
    print(f"  FORMULA: round2(item_disc_total - header_discount + line_tax_total + header_tax + other_charges)")
    print(f"         = round2({item_disc_total} - {header_discount} + {line_tax_total} + {header_tax} + {other_charges})")
    print(f"         = round2({pre_round})")
    print(f"  will_book_gross        = {will_book}")
    print()

    erp_res = erp_book(p)
    print(f"  erp_book() returned    = {erp_res}")

    target_s = str(p.get("gross_total") or "").strip()
    try:
        target_g = float(target_s) if target_s else 0.0
    except ValueError:
        target_g = 0.0
    diff = erp_res["will_book_gross"] - target_g
    status = "PASS" if abs(diff) < 0.05 else "FAIL"
    print(f"  printed gross_total    = {target_s!r}  => parsed as {target_g}")
    print(f"  diff (booked-printed)  = {diff}")
    print(f"  STATUS                 = {status}")

    print()
    print("--- RAW PAYABLE JSON ---")
    print(json.dumps(p, indent=2, ensure_ascii=False))
    print()


def main():
    for fname, idxs in TARGETS.items():
        data = json.loads((OUT / fname).read_text(encoding="utf-8"))
        payables = data.get("payables", [])
        for idx in idxs:
            suffix = f".{idx}" if idx > 0 else ""
            label = fname.replace(".json", "") + suffix
            diagnose(label, payables[idx])


main()
