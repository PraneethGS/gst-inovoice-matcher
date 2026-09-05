"""
Unit tests for the reconciliation engine. Run with:
    python -m pytest tests/ -v
from the project root (after `pip install pytest` alongside requirements.txt).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.matcher import reconcile


def base_row(**overrides):
    row = {
        "gstin": "29ABCDE1234F1ZW",
        "supplier_name": "Sunrise Traders",
        "invoice_number": "INV-1001",
        "invoice_date": "2026-07-10",
        "taxable_value": "10000",
        "tax_amount": "1800",
        "total_value": "11800",
    }
    row.update(overrides)
    return row


def test_clean_match():
    g = [base_row()]
    l = [base_row()]
    report = reconcile(g, l)
    assert report["matched_count"] == 1
    assert report["match_rate"] == 1.0
    assert report["results"][0]["status"] == "MATCHED"


def test_invalid_gstin_in_gstr2b_is_flagged():
    report = reconcile([base_row(gstin="INVALID")], [base_row()])
    assert report["results"][0]["status"] == "INVALID_GSTIN"
    assert report["counts_by_status"]["INVALID_GSTIN"] == 1
    assert report["matched_count"] == 0


def test_invalid_gstin_in_ledger_is_flagged():
    report = reconcile([base_row()], [base_row(gstin="29ABCDE1234F1Z5")])
    assert report["results"][0]["status"] == "INVALID_GSTIN"
    assert report["matched_count"] == 0


def test_amount_mismatch_detected():
    g = [base_row(taxable_value="10000")]
    l = [base_row(taxable_value="9000")]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "AMOUNT_MISMATCH"
    assert report["match_rate"] == 0.0


def test_small_rounding_within_tolerance_still_matches():
    g = [base_row(taxable_value="10000.00")]
    l = [base_row(taxable_value="10002.00")]  # 0.02% drift, under 0.5% tolerance
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "MATCHED"


def test_missing_in_ledger_flags_itc_risk():
    g = [base_row(tax_amount="1800")]
    l = []
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "MISSING_IN_LEDGER"
    assert report["itc_at_risk_amount"] == 1800.0


def test_missing_in_2b():
    g = []
    l = [base_row()]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "MISSING_IN_2B"


def test_duplicate_in_ledger():
    g = [base_row()]
    l = [base_row(), base_row()]
    report = reconcile(g, l)
    statuses = [r["status"] for r in report["results"]]
    assert "DUPLICATE_IN_LEDGER" in statuses
    # the second ledger copy should NOT silently disappear or double count as matched
    assert report["matched_count"] == 0


def test_fuzzy_invoice_typo_match():
    g = [base_row(invoice_number="INV-1001")]
    l = [base_row(invoice_number="INV1001A")]  # typo, same GSTIN + amount
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "FUZZY_MATCH"


def test_fuzzy_invoice_format_change_is_not_reported_as_exact_match():
    report = reconcile([base_row(invoice_number="INV-1001")], [base_row(invoice_number="INV1001")])
    assert report["results"][0]["status"] == "FUZZY_MATCH"


def test_invalid_invoice_date_is_flagged():
    report = reconcile([base_row(invoice_date="2026-02-30")], [base_row()])
    assert report["results"][0]["status"] == "INVALID_DATA"


def test_invalid_gstin_checksum_is_flagged():
    report = reconcile([base_row(gstin="29ABCDE1234F1Z5")], [])
    assert report["results"][0]["status"] == "INVALID_GSTIN"


def test_date_mismatch():
    g = [base_row(invoice_date="2026-07-10")]
    l = [base_row(invoice_date="2026-07-15")]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "DATE_MISMATCH"


def test_cross_period_match_is_classified_separately():
    g = [base_row(invoice_date="2026-08-01")]
    l = [base_row(invoice_date="2026-07-31")]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "CROSS_PERIOD_MATCH"
    assert report["matched_count"] == 0


def test_cross_period_date_mismatch_from_evaluation_fixture_is_detected():
    g = [base_row(invoice_number="INV-1043", invoice_date="2026-07-28")]
    l = [base_row(invoice_number="INV-1043", invoice_date="2026-08-02")]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "CROSS_PERIOD_MATCH"


def test_credit_note_is_netted_against_invoice():
    g = [base_row(taxable_value="9000", tax_amount="1620", total_value="10620")]
    l = [
        base_row(taxable_value="10000", tax_amount="1800", total_value="11800"),
        base_row(
            invoice_number="CN-1001", document_type="credit_note",
            reference_invoice="INV-1001", taxable_value="1000",
            tax_amount="180", total_value="1180",
        ),
    ]
    report = reconcile(g, l)
    assert report["results"][0]["status"] == "MATCHED"
    assert "netting" in report["results"][0]["reason"]


def test_itc_risk_uses_post_netting_tax_amount():
    rows = [
        base_row(taxable_value="10000", tax_amount="1800", total_value="11800"),
        base_row(
            invoice_number="CN-1001", document_type="credit_note",
            reference_invoice="INV-1001", taxable_value="1000",
            tax_amount="180", total_value="1180",
        ),
    ]
    report = reconcile(rows, [])
    assert report["itc_at_risk_amount"] == 1620.0


def test_match_rate_math_on_mixed_batch():
    g = [base_row(invoice_number="INV-A"), base_row(invoice_number="INV-B")]
    l = [base_row(invoice_number="INV-A")]  # only one matches, other missing in ledger
    report = reconcile(g, l)
    assert report["total_records_processed"] == 2
    assert report["matched_count"] == 1
    assert report["match_rate"] == 0.5
