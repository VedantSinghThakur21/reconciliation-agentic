from __future__ import annotations
"""
Simplified HITL review / exception queue for reconq-core demos.

In-memory only — no database, no multi-tenant config. Holds low-confidence or
materiality-gated matches for a human to confirm or reject.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

Decision = Literal["pending", "confirmed", "rejected"]


@dataclass
class ReviewItem:
    id: str
    payment_id: str
    invoice_id: str | None
    confidence: float
    reason: str
    reasoning: str
    amount_applied: float
    status: Decision = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: str | None = None


class ReviewQueue:
    """In-memory exception queue for HITL demo."""

    def __init__(self) -> None:
        self._items: dict[str, ReviewItem] = {}

    def enqueue_from_match(
        self,
        *,
        payment_id: str,
        invoice_id: str | None,
        confidence: float,
        reason: str,
        reasoning: str,
        amount_applied: float,
    ) -> ReviewItem:
        item = ReviewItem(
            id=f"REV-{uuid.uuid4().hex[:8].upper()}",
            payment_id=payment_id,
            invoice_id=invoice_id,
            confidence=confidence,
            reason=reason or "pending_review",
            reasoning=reasoning,
            amount_applied=amount_applied,
        )
        self._items[item.id] = item
        logger.info("[ReviewQueue] enqueued %s payment=%s reason=%s", item.id, payment_id, reason)
        return item

    def confirm(self, review_id: str) -> ReviewItem:
        item = self._require(review_id)
        item.status = "confirmed"
        item.decided_at = datetime.now(timezone.utc).isoformat()
        return item

    def reject(self, review_id: str) -> ReviewItem:
        item = self._require(review_id)
        item.status = "rejected"
        item.decided_at = datetime.now(timezone.utc).isoformat()
        return item

    def pending(self) -> list[ReviewItem]:
        return [i for i in self._items.values() if i.status == "pending"]

    def all(self) -> list[ReviewItem]:
        return list(self._items.values())

    def as_dicts(self) -> list[dict[str, Any]]:
        return [i.__dict__.copy() for i in self._items.values()]

    def _require(self, review_id: str) -> ReviewItem:
        if review_id not in self._items:
            raise KeyError(f"Unknown review id: {review_id}")
        return self._items[review_id]
