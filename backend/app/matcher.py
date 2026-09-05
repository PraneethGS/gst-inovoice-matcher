"""
Reconciliation engine: matches GSTR-2B rows against the company's purchase
ledger rows and classifies every record into one of:

  MATCHED             - exact match on GSTIN + invoice number, amounts agree
  AMOUNT_MISMATCH     - same invoice, taxable/tax value differs beyond tolerance
  DATE_MISMATCH       - same invoice/amount, invoice date differs within a period
  CROSS_PERIOD_MATCH  - same invoice/amount, but the records belong to different months
  FUZZY_MATCH         - matched via invoice-number typo tolerance (needs review)
  DUPLICATE_IN_LEDGER - ledger has more than one row for the same GSTIN+invoice
  MISSING_IN_LEDGER   - present in GSTR-2B, not booked in the ledger (credit at risk)
  MISSING_IN_2B       - present in ledger, supplier hasn't filed it yet

This mirrors how an accountant actually works through the GSTR-2B vs
purchase-register mismatch report, just automated and with an audit trail.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import date
import re
from typing import List, Dict, Optional

AMOUNT_TOLERANCE_PCT = 0.005  # 0.5% rounding tolerance before flagging a mismatch
FUZZY_INVOICE_THRESHOLD = 0.82  # similarity ratio to consider a typo-match
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9][A-Z][A-Z0-9]$")
NOTE_TYPES = {"credit", "credit_note", "credit note", "debit", "debit_note", "debit note"}


@dataclass
class MatchResult:
    status: str
    gstin: str
    supplier_name: str
    gstr2b_invoice: Optional[str]
    ledger_invoice: Optional[str]
    taxable_value_2b: Optional[float]
    taxable_value_ledger: Optional[float]
    reason: str
    confidence: float
    tax_amount_2b: Optional[float] = None
    tax_amount_ledger: Optional[float] = None


def _norm_invoice(inv: str) -> str:
    return inv.upper().replace("-", "").replace(" ", "").strip()


def _base36_value(char: str) -> int:
    return int(char) if char.isdigit() else ord(char) - ord("A") + 10


def _base36_char(value: int) -> str:
    return str(value) if value < 10 else chr(ord("A") + value - 10)


def gstin_checksum(gstin: str) -> str:
    """Calculate the GSTIN checksum for the first 14 characters."""
    total = 0
    factor = 2
    for char in reversed(gstin[:14]):
        product = _base36_value(char) * factor
        total += product // 36 + product % 36
        factor = 1 if factor == 2 else 2
    return _base36_char((36 - total % 36) % 36)


def is_valid_gstin(gstin: str) -> bool:
    """Return whether a GSTIN has valid structure and checksum."""
    normalized = str(gstin).strip().upper()
    return bool(GSTIN_PATTERN.fullmatch(normalized)) and normalized[-1] == gstin_checksum(normalized)


def is_valid_invoice_date(value: str) -> bool:
    try:
        date.fromisoformat(str(value).strip())
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()))
    except (TypeError, ValueError):
        return False


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_invoice(a), _norm_invoice(b)).ratio()


def _amounts_close(a: float, b: float, tolerance_pct: float = AMOUNT_TOLERANCE_PCT) -> bool:
    if a == 0 and b == 0:
        return True
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base <= tolerance_pct


def _net_documents(rows: List[Dict]) -> List[Dict]:
    """Net optional credit/debit-note rows against their referenced invoice.

    Existing imports have no document metadata and pass through unchanged. A
    note may use positive values; its document type determines the sign.
    """
    grouped: Dict[tuple, Dict] = {}
    note_keys = set()
    for source in rows:
        document_type = str(source.get("document_type", "invoice")).strip().lower()
        reference = str(source.get("reference_invoice", "")).strip()
        if document_type in NOTE_TYPES or reference:
            invoice_number = reference if reference else source.get("invoice_number", "")
            note_keys.add((str(source.get("gstin", "")).strip().upper(), _norm_invoice(invoice_number)))

    for row_index, source in enumerate(rows):
        row = dict(source)
        document_type = str(row.get("document_type", "invoice")).strip().lower()
        reference = str(row.get("reference_invoice", "")).strip()
        is_note = document_type in NOTE_TYPES or bool(reference)
        invoice_number = reference if is_note and reference else row.get("invoice_number", "")
        base_key = (str(row.get("gstin", "")).strip().upper(), _norm_invoice(invoice_number))
        # Keep ordinary duplicate rows separate. Only an invoice referenced by
        # a note is intentionally collapsed into a net position.
        if not is_note and base_key not in note_keys:
            row["_netted"] = False
            for column in ("taxable_value", "tax_amount", "total_value"):
                row[column] = float(row.get(column, 0) or 0)
            grouped[(base_key, row_index)] = row
            continue
        key = base_key
        if key not in grouped:
            grouped[key] = dict(row, invoice_number=invoice_number, _netted=False)
            for column in ("taxable_value", "tax_amount", "total_value"):
                grouped[key][column] = float(row.get(column, 0) or 0)
        else:
            target = grouped[key]
            for column in ("taxable_value", "tax_amount", "total_value"):
                target[column] += float(row.get(column, 0) or 0)
        if is_note:
            target = grouped[key]
            sign = -1 if "credit" in document_type else 1
            for column in ("taxable_value", "tax_amount", "total_value"):
                value = float(row.get(column, 0) or 0)
                # The first note was initially added above, so replace its
                # contribution with the correctly signed amount.
                target[column] -= value
                target[column] += sign * abs(value)
            target["_netted"] = True
    return list(grouped.values())


def _different_period(first: str, second: str) -> bool:
    return str(first or "")[:7] != str(second or "")[:7]


def reconcile(
    gstr2b_rows: List[Dict],
    ledger_rows: List[Dict],
    amount_tolerance_pct: float = AMOUNT_TOLERANCE_PCT,
    fuzzy_invoice_threshold: float = FUZZY_INVOICE_THRESHOLD,
) -> Dict:
    results: List[MatchResult] = []

    gstr2b_rows = _net_documents(gstr2b_rows)
    ledger_rows = _net_documents(ledger_rows)

    valid_gstr2b_rows = []
    for row in gstr2b_rows:
        if not is_valid_invoice_date(row.get("invoice_date", "")):
            results.append(MatchResult(
                status="INVALID_DATA", gstin=str(row.get("gstin", "")),
                supplier_name=row.get("supplier_name", ""),
                gstr2b_invoice=row.get("invoice_number"), ledger_invoice=None,
                taxable_value_2b=float(row.get("taxable_value", 0) or 0), taxable_value_ledger=None,
                reason="Invoice date is not a valid YYYY-MM-DD calendar date.", confidence=0.0,
            ))
        elif is_valid_gstin(row.get("gstin", "")):
            valid_gstr2b_rows.append(row)
        else:
            results.append(MatchResult(
                status="INVALID_GSTIN", gstin=str(row.get("gstin", "")),
                supplier_name=row.get("supplier_name", ""),
                gstr2b_invoice=row.get("invoice_number"), ledger_invoice=None,
                taxable_value_2b=float(row.get("taxable_value", 0) or 0),
                taxable_value_ledger=None,
                reason="GSTIN does not match the standard 15-character format.",
                confidence=0.0,
            ))

    valid_ledger_rows = []
    for row in ledger_rows:
        if not is_valid_invoice_date(row.get("invoice_date", "")):
            results.append(MatchResult(
                status="INVALID_DATA", gstin=str(row.get("gstin", "")),
                supplier_name=row.get("supplier_name", ""),
                gstr2b_invoice=None, ledger_invoice=row.get("invoice_number"),
                taxable_value_2b=None, taxable_value_ledger=float(row.get("taxable_value", 0) or 0),
                reason="Invoice date is not a valid YYYY-MM-DD calendar date.", confidence=0.0,
            ))
        elif is_valid_gstin(row.get("gstin", "")):
            valid_ledger_rows.append(row)
        else:
            results.append(MatchResult(
                status="INVALID_GSTIN", gstin=str(row.get("gstin", "")),
                supplier_name=row.get("supplier_name", ""),
                gstr2b_invoice=None, ledger_invoice=row.get("invoice_number"),
                taxable_value_2b=None,
                taxable_value_ledger=float(row.get("taxable_value", 0) or 0),
                reason="GSTIN does not match the standard 15-character format.",
                confidence=0.0,
            ))

    gstr2b_rows = valid_gstr2b_rows
    ledger_rows = valid_ledger_rows

    # index ledger rows by (gstin, exact invoice) to find duplicates cheaply
    ledger_by_key: Dict[tuple, List[Dict]] = {}
    for row in ledger_rows:
        key = (row["gstin"], _norm_invoice(row["invoice_number"]))
        ledger_by_key.setdefault(key, []).append(row)

    matched_ledger_ids = set()  # track which ledger rows got consumed

    def ledger_row_id(row):
        return id(row)

    for g in gstr2b_rows:
        key = (g["gstin"], _norm_invoice(g["invoice_number"]))
        candidates = ledger_by_key.get(key, [])
        available = [r for r in candidates if ledger_row_id(r) not in matched_ledger_ids]

        if available:
            l = available[0]
            matched_ledger_ids.add(ledger_row_id(l))

            if len(candidates) > 1:
                results.append(MatchResult(
                    status="DUPLICATE_IN_LEDGER",
                    gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=f"Ledger contains {len(candidates)} entries for invoice {g['invoice_number']} "
                           f"from {g['supplier_name']} — likely booked twice. Only one claimable in ITC.",
                    confidence=0.95,
                ))
                continue

            taxable_ok = _amounts_close(
                float(g["taxable_value"]), float(l["taxable_value"]), amount_tolerance_pct
            )
            date_ok = g["invoice_date"] == l["invoice_date"]

            if taxable_ok and date_ok and str(g["invoice_number"]).strip() != str(l["invoice_number"]).strip():
                matched_ledger_ids.add(ledger_row_id(l))
                score = _similar(g["invoice_number"], l["invoice_number"])
                results.append(MatchResult(
                    status="FUZZY_MATCH", gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=f"Invoice formatting differs but normalized numbers match ({score:.0%} similarity); "
                           "flagged for human confirmation.", confidence=round(score, 2),
                ))
                continue

            if taxable_ok and date_ok:
                status = "MATCHED"
                reason = "Invoice number, GSTIN, amount and date all agree."
                if g.get("_netted") or l.get("_netted"):
                    reason = "Invoice and GSTIN agree after netting the related credit/debit notes."
                results.append(MatchResult(
                    status=status,
                    gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=reason,
                    confidence=1.0,
                ))
            elif not taxable_ok:
                diff = round(float(g["taxable_value"]) - float(l["taxable_value"]), 2)
                results.append(MatchResult(
                    status="AMOUNT_MISMATCH",
                    gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=f"Taxable value differs by ₹{abs(diff):,.2f} "
                           f"({'2B higher' if diff > 0 else 'ledger higher'}) — check for a credit note "
                           f"or a data-entry error before claiming ITC.",
                    confidence=0.9,
                ))
            elif _different_period(g["invoice_date"], l["invoice_date"]):
                results.append(MatchResult(
                    status="CROSS_PERIOD_MATCH", gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=f"Invoice and amount match, but dates cross periods ({g['invoice_date']} vs "
                           f"{l['invoice_date']}) — review the correct return period.",
                    confidence=0.9,
                ))
            else:
                results.append(MatchResult(
                    status="DATE_MISMATCH",
                    gstin=g["gstin"], supplier_name=g["supplier_name"],
                    gstr2b_invoice=g["invoice_number"], ledger_invoice=l["invoice_number"],
                    taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(l["taxable_value"]),
                    reason=f"Amounts match but invoice date differs ({g['invoice_date']} vs {l['invoice_date']}) "
                           f"— confirm which date is correct for the return period.",
                    confidence=0.85,
                ))
            continue

        # no exact key match — try fuzzy invoice-number match within same GSTIN
        best, best_score = None, 0.0
        for l in ledger_rows:
            if ledger_row_id(l) in matched_ledger_ids:
                continue
            if l["gstin"] != g["gstin"]:
                continue
            score = _similar(g["invoice_number"], l["invoice_number"])
            if score > best_score:
                best, best_score = l, score

        if best is not None and best_score >= fuzzy_invoice_threshold and _amounts_close(
            float(g["taxable_value"]), float(best["taxable_value"]), amount_tolerance_pct
        ):
            matched_ledger_ids.add(ledger_row_id(best))
            results.append(MatchResult(
                status="FUZZY_MATCH",
                gstin=g["gstin"], supplier_name=g["supplier_name"],
                gstr2b_invoice=g["invoice_number"], ledger_invoice=best["invoice_number"],
                taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=float(best["taxable_value"]),
                reason=f"Invoice numbers '{g['invoice_number']}' and '{best['invoice_number']}' are "
                       f"{best_score:.0%} similar with matching amount — likely a typo, not a genuine miss. "
                       f"Flagged for a human to confirm before auto-accepting.",
                confidence=round(best_score, 2),
            ))
            continue

        # genuinely missing from the ledger
        results.append(MatchResult(
            status="MISSING_IN_LEDGER",
            gstin=g["gstin"], supplier_name=g["supplier_name"],
            gstr2b_invoice=g["invoice_number"], ledger_invoice=None,
            taxable_value_2b=float(g["taxable_value"]), taxable_value_ledger=None,
            reason=f"Supplier {g['supplier_name']} filed this invoice in GSTR-2B but it is not booked in the "
                   f"purchase ledger — input tax credit of ₹{float(g['tax_amount']):,.2f} is at risk if unclaimed.",
            confidence=0.98,
        ))

    # anything left in the ledger that was never consumed is missing in 2B
    for l in ledger_rows:
        if ledger_row_id(l) in matched_ledger_ids:
            continue
        results.append(MatchResult(
            status="MISSING_IN_2B",
            gstin=l["gstin"], supplier_name=l["supplier_name"],
            gstr2b_invoice=None, ledger_invoice=l["invoice_number"],
            taxable_value_2b=None, taxable_value_ledger=float(l["taxable_value"]),
            reason=f"Booked in the purchase ledger but {l['supplier_name']} has not yet filed this invoice "
                   f"in their GSTR-1 — ITC cannot be claimed until it appears in GSTR-2B.",
            confidence=0.9,
        ))

    gstr2b_tax_by_invoice = {
        (row["gstin"], _norm_invoice(row["invoice_number"])): float(row.get("tax_amount", 0) or 0)
        for row in gstr2b_rows
    }
    ledger_tax_by_invoice = {
        (row["gstin"], _norm_invoice(row["invoice_number"])): float(row.get("tax_amount", 0) or 0)
        for row in ledger_rows
    }
    for result in results:
        if result.gstr2b_invoice:
            result.tax_amount_2b = gstr2b_tax_by_invoice.get(
                (result.gstin, _norm_invoice(result.gstr2b_invoice))
            )
        if result.ledger_invoice:
            result.tax_amount_ledger = ledger_tax_by_invoice.get(
                (result.gstin, _norm_invoice(result.ledger_invoice))
            )

    total = len(results)
    matched = sum(1 for r in results if r.status == "MATCHED")
    exceptions = [r for r in results if r.status != "MATCHED"]

    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    itc_at_risk = sum(
        max(0.0, float(g["tax_amount"])) for g in gstr2b_rows
        if any(r.status == "MISSING_IN_LEDGER" and r.gstr2b_invoice == g["invoice_number"]
               and r.gstin == g["gstin"] for r in results)
    )

    return {
        "total_records_processed": total,
        "matched_count": matched,
        "match_rate": round(matched / total, 4) if total else 0.0,
        "exception_count": len(exceptions),
        "counts_by_status": by_status,
        "itc_at_risk_amount": round(itc_at_risk, 2),
        "matching_settings": {
            "amount_tolerance_pct": round(amount_tolerance_pct * 100, 3),
            "fuzzy_invoice_threshold": round(fuzzy_invoice_threshold * 100, 1),
        },
        "results": [r.__dict__ for r in results],
    }
