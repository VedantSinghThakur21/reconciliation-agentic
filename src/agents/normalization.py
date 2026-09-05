from __future__ import annotations

import re
from typing import Any


_SUFFIXES = re.compile(
    r"\b(ltd|limited|inc|incorporated|corp|corporation|llc|pvt|private|co|company|solutions|enterprises?)\b",
    re.I,
)
_NOISE = re.compile(r"[^a-z0-9\s]")


def normalize_party_name(name: str) -> str:
    text = (name or "").strip().lower()
    text = _NOISE.sub(" ", text)
    text = _SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_reference(ref: str) -> str:
    return re.sub(r"[\s\-_/]", "", (ref or "").strip().upper())


def normalize_currency(ccy: str) -> str:
    c = (ccy or "INR").strip().upper()
    aliases = {"RS": "INR", "INR.": "INR", "USD.": "USD", "$": "USD"}
    return aliases.get(c, c)


def normalize_invoice(inv: dict[str, Any]) -> dict[str, Any]:
    out = dict(inv)
    out["customerName"] = (inv.get("customerName") or "").strip()
    out["normalizedName"] = normalize_party_name(out["customerName"])
    out["referenceNumber"] = (inv.get("referenceNumber") or inv.get("id") or "").strip()
    out["normalizedRef"] = normalize_reference(out["referenceNumber"])
    out["currency"] = normalize_currency(str(inv.get("currency") or "INR"))
    out["amount"] = round(float(inv.get("amount") or 0), 2)
    out["remaining_balance"] = round(float(inv.get("remaining_balance", out["amount"])), 2)
    return out


def normalize_payment(pay: dict[str, Any]) -> dict[str, Any]:
    out = dict(pay)
    out["senderName"] = (pay.get("senderName") or "").strip()
    out["normalizedName"] = normalize_party_name(out["senderName"])
    out["reference"] = (pay.get("reference") or "").strip()
    out["normalizedRef"] = normalize_reference(out["reference"])
    out["currency"] = normalize_currency(str(pay.get("currency") or "INR"))
    out["amount"] = round(float(pay.get("amount") or 0), 2)
    return out


def normalize_batch(
    invoices: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [normalize_invoice(i) for i in invoices], [normalize_payment(p) for p in payments]
