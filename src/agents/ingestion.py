from __future__ import annotations

from typing import Any


def validate_invoices(invoices: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    ok: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[str] = set()
    for inv in invoices:
        iid = str(inv.get("id") or "")
        if not iid:
            issues.append("Invoice missing id — skipped")
            continue
        if iid in seen:
            issues.append(f"Duplicate invoice id {iid}")
            continue
        if float(inv.get("amount") or 0) <= 0:
            issues.append(f"Invoice {iid} has non-positive amount")
            continue
        seen.add(iid)
        ok.append(inv)
    return ok, issues


def validate_payments(payments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    ok: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[str] = set()
    for pay in payments:
        pid = str(pay.get("id") or "")
        if not pid:
            issues.append("Payment missing id — skipped")
            continue
        if pid in seen:
            issues.append(f"Duplicate payment id {pid} — flagged")
        if float(pay.get("amount") or 0) <= 0:
            issues.append(f"Payment {pid} has non-positive amount")
            continue
        seen.add(pid)
        ok.append(pay)
    return ok, issues
