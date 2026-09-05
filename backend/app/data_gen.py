"""
Synthetic data generator for the GST Invoice Matching Agent.

Produces two datasets that mimic the real-world reconciliation problem:
  1. gstr2b.csv        -> auto-populated purchase data pulled from the GST portal
  2. purchase_ledger.csv -> the company's own books/purchase register

In the real world these two should agree, but in practice they diverge because
of supplier filing delays, typos, amount rounding, duplicate entries, or
invoices the company simply never recorded. This generator deliberately
injects each of those failure modes so the matcher has something real to do,
and so the reported precision/recall numbers mean something.
"""
import csv
import random
from datetime import date, timedelta

random.seed(42)

SUPPLIERS = [
    ("29ABCDE1234F1ZW", "Sunrise Traders"),
    ("07PQRSX5678K1Z0", "Metro Packaging Pvt Ltd"),
    ("27LMNOQ9988P1Z6", "BrightSteel Industries"),
    ("06WXYZA1122R1ZY", "Delta Logistics"),
    ("33FGHIJ4455T1Z1", "Coastal Chemicals Co"),
    ("19KLMNP7766U1ZC", "Eastern Paper Mills"),
    ("24QRSTU3344V1ZW", "Vardhan Electricals"),
    ("36ABXYZ9911W1Z9", "Golden Foods Distributors"),
]

N_BASE_INVOICES = 60


def _rand_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def generate(out_dir: str):
    period_start = date(2026, 7, 1)
    period_end = date(2026, 7, 31)

    gstr2b_rows = []
    ledger_rows = []
    ground_truth_rows = []

    for i in range(1, N_BASE_INVOICES + 1):
        gstin, supplier = random.choice(SUPPLIERS)
        inv_no = f"INV-{1000 + i}"
        inv_date = _rand_date(period_start, period_end)
        taxable_value = round(random.uniform(5000, 250000), 2)
        gst_rate = random.choice([5, 12, 18, 28])
        tax_amount = round(taxable_value * gst_rate / 100, 2)
        total = round(taxable_value + tax_amount, 2)

        scenario = random.choices(
            population=[
                "clean_match",
                "amount_mismatch",
                "missing_in_ledger",
                "missing_in_2b",
                "duplicate_in_ledger",
                "invoice_number_typo",
                "date_mismatch",
            ],
            weights=[45, 15, 10, 10, 5, 10, 5],
            k=1,
        )[0]
        ground_truth_rows.append({
            "gstin": gstin,
            "invoice_number": inv_no,
            "scenario": scenario,
        })

        gstr2b_rows.append({
            "gstin": gstin,
            "supplier_name": supplier,
            "invoice_number": inv_no,
            "invoice_date": inv_date.isoformat(),
            "taxable_value": taxable_value,
            "tax_amount": tax_amount,
            "total_value": total,
        })

        if scenario == "clean_match":
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })

        elif scenario == "amount_mismatch":
            # accountant recorded a slightly different taxable value (rounding /
            # partial credit note not yet reflected)
            drift = round(taxable_value * random.uniform(0.01, 0.06), 2)
            new_taxable = round(taxable_value - drift, 2)
            new_tax = round(new_taxable * gst_rate / 100, 2)
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": new_taxable,
                "tax_amount": new_tax,
                "total_value": round(new_taxable + new_tax, 2),
            })

        elif scenario == "missing_in_ledger":
            # supplier filed it, company never booked it (common: input credit lost)
            pass

        elif scenario == "missing_in_2b":
            # company recorded it, supplier hasn't filed GSTR-1 yet
            gstr2b_rows.pop()  # remove the row we just added to 2B
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })

        elif scenario == "duplicate_in_ledger":
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })

        elif scenario == "invoice_number_typo":
            typo_no = inv_no.replace("INV-", "INV") if random.random() < 0.5 else inv_no + "A"
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": typo_no,
                "invoice_date": inv_date.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })

        elif scenario == "date_mismatch":
            shifted = inv_date + timedelta(days=random.choice([-3, -2, 2, 3, 5]))
            ledger_rows.append({
                "gstin": gstin,
                "supplier_name": supplier,
                "invoice_number": inv_no,
                "invoice_date": shifted.isoformat(),
                "taxable_value": taxable_value,
                "tax_amount": tax_amount,
                "total_value": total,
            })

    random.shuffle(gstr2b_rows)
    random.shuffle(ledger_rows)

    fieldnames = ["gstin", "supplier_name", "invoice_number", "invoice_date",
                  "taxable_value", "tax_amount", "total_value"]

    with open(f"{out_dir}/gstr2b.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(gstr2b_rows)

    with open(f"{out_dir}/purchase_ledger.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ledger_rows)

    with open(f"{out_dir}/ground_truth.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["gstin", "invoice_number", "scenario"])
        writer.writeheader()
        writer.writerows(ground_truth_rows)

    return len(gstr2b_rows), len(ledger_rows)


if __name__ == "__main__":
    import os
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    os.makedirs(out_dir, exist_ok=True)
    n2b, nledger = generate(out_dir)
    print(f"Generated {n2b} GSTR-2B rows and {nledger} ledger rows in {os.path.normpath(out_dir)}/")
