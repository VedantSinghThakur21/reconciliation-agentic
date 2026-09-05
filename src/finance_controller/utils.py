from __future__ import annotations

import csv
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

MERCHANT_ALIASES = {
    "AMZN": "Amazon",
    "GCP": "Google Cloud",
    "ORCL": "Oracle",
    "SFDC": "Salesforce",
    "MSFT": "Microsoft",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "WIPRO": "Wipro",
    "RELIANCE": "Reliance Industries",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_merchant(name: str) -> str:
    raw = (name or "").strip()
    upper = re.sub(r"[^A-Z0-9]", "", raw.upper())
    for alias, full in MERCHANT_ALIASES.items():
        if upper == alias or upper.startswith(alias):
            return full
    # strip corp suffixes
    cleaned = re.sub(
        r"\b(ltd|limited|inc|pvt|private|corp|corporation|llc|co|company)\b",
        "",
        raw,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", cleaned).strip().title() or raw


def normalize_reference(ref: str) -> str:
    return re.sub(r"[\s\-_/]", "", (ref or "").strip().upper())


def normalize_currency(ccy: str) -> str:
    c = (ccy or "INR").strip().upper()
    return {"RS": "INR", "₹": "INR", "$": "USD"}.get(c, c)


def read_csv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


# Demo AR batch size for finance-controller eval (must stay in sync with
# scripts/generate_fc_demo_data.py). Track requirement: 50+ records.
DEMO_ERP_COUNT = 51


def stub_erp_transactions(n: int | None = None, mode: str = "AR") -> list[dict[str, Any]]:
    if n is None:
        n = DEMO_ERP_COUNT
    companies = [
        "Amazon", "Google Cloud", "Oracle", "Salesforce", "Microsoft",
        "Tata Consultancy Services", "Infosys", "Wipro", "Reliance Industries",
        "HDFC Bank", "ICICI Bank", "Flipkart", "Zomato", "Swiggy",
        "Paytm", "PhonePe", "Nykaa", "DMart", "Asian Paints", "Maruti Suzuki",
        "Titan", "Bajaj Auto", "Hindustan Unilever", "ITC",
        "Kotak Mahindra", "Axis Bank", "Jio Platforms", "Myntra",
        "Ola Electric", "BharatPe", "Razorpay", "Freshworks",
        "Zoho", "Persistent Systems", "L&T", "Adani Ports",
        "Britannia", "Nestle India", "Sun Pharma", "Dr Reddy",
        "Cipla", "Biocon", "HCL Tech", "Tech Mahindra",
        "Mindtree", "Lupin", "Godrej", "Mahindra & Mahindra",
        "TVS Motor", "Hero MotoCorp", "Eicher Motors",
    ]
    base = date(2026, 3, 1)
    rows = []
    for i in range(n):
        merchant = companies[i % len(companies)]
        amt = round(2500 + (i * 137.35) % 22000, 2)
        # sprinkle partial / special amounts for later strategies
        if i % 11 == 0:
            amt = round(amt * 2, 2)
        d = base + timedelta(days=i)
        rows.append({
            "transaction_id": f"ERP-{1001 + i}",
            "date": d.isoformat(),
            "amount": amt,
            "currency": "INR",
            "merchant": merchant,
            "reference": f"REF-{merchant[:4].upper()}-{1001 + i}",
            "description": f"{'AR invoice' if mode != 'AP' else 'AP bill'} {merchant}",
            "source": "erp",
        })
    return rows


def amount_score(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 1.0
    denom = max(abs(a), abs(b), 0.01)
    diff_pct = abs(a - b) / denom
    if diff_pct <= 1e-9:
        return 1.0
    if diff_pct >= 0.05:
        return 0.0
    return max(0.0, 1.0 - (diff_pct / 0.05))


def date_score(d1: date | None, d2: date | None) -> float:
    if not d1 or not d2:
        return 0.5
    days = abs((d1 - d2).days)
    return max(0.0, 1.0 - days * 0.1)


def reference_score(r1: str, r2: str) -> float:
    a, b = normalize_reference(r1), normalize_reference(r2)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.5
    return 0.0


def merchant_score(m1: str, m2: str) -> float:
    return fuzz.token_sort_ratio(m1 or "", m2 or "") / 100.0


def confidence_score(amount_s: float, merchant_s: float, date_s: float, ref_s: float) -> float:
    return round(0.35 * amount_s + 0.25 * merchant_s + 0.20 * date_s + 0.20 * ref_s, 4)
