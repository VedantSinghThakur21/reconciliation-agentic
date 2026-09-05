from __future__ import annotations
"""Double-entry verification helpers (no GL posting)."""

from decimal import Decimal
from typing import Any, Protocol


class JournalLineLike(Protocol):
    debit: Any
    credit: Any


def _to_decimal(val) -> Decimal:
    if val is None:
        return Decimal("0.00")
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0.00")


def validate_balanced_entry(lines: list[Any]) -> bool:
    """
    Verifies that the sum of debits equals the sum of credits (within 0.01 tolerance).

    Accepts objects with `.debit`/`.credit` attributes or dicts with those keys.
    Full GL posting is out of scope for reconq-core — this is the invariant check only.
    """
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")
    for line in lines:
        if isinstance(line, dict):
            total_debits += _to_decimal(line.get("debit"))
            total_credits += _to_decimal(line.get("credit"))
        else:
            total_debits += _to_decimal(getattr(line, "debit", 0))
            total_credits += _to_decimal(getattr(line, "credit", 0))
    return abs(total_debits - total_credits) < Decimal("0.01")
