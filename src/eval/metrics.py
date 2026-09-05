from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def load_ground_truth(path: str | Path) -> list[dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def evaluate_against_ground_truth(
    decisions: list[dict[str, Any]],
    ground_truth: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Compare pipeline decisions to labeled ground truth.

    A true positive = predicted invoice matches expected invoice (both non-empty)
    and expected_status is reconciled (or we treat matched invoice as positive).
    """
    by_payment = {str(d.get("paymentId")): d for d in decisions}
    tp = fp = fn = tn = 0
    details: list[dict[str, Any]] = []

    for gt in ground_truth:
        pid = gt.get("payment_id") or gt.get("paymentId") or ""
        expected_inv = (gt.get("expected_invoice_id") or gt.get("invoice_id") or "").strip() or None
        expected_status = (gt.get("expected_status") or "").strip().lower()
        pred = by_payment.get(pid)
        pred_inv = (pred.get("invoiceId") if pred else None) or None
        pred_status = (pred.get("status") if pred else "missing")

        expected_match = expected_inv is not None and expected_status == "reconciled"
        predicted_match = pred_inv is not None and pred_status == "reconciled"

        correct = False
        if expected_match and predicted_match:
            if pred_inv == expected_inv:
                tp += 1
                correct = True
            else:
                fp += 1
                fn += 1
        elif expected_match and not predicted_match:
            fn += 1
        elif not expected_match and predicted_match:
            fp += 1
        else:
            # both non-reconciled — treat as true negative if statuses align-ish
            tn += 1
            correct = (pred_status == expected_status) or (
                expected_status == "pending_review" and pred_status in ("pending_review", "unreconciled")
            )
            # If both expect a specific invoice under review, check id
            if expected_inv and pred_inv and expected_inv == pred_inv:
                correct = True

        details.append({
            "payment_id": pid,
            "expected_invoice_id": expected_inv,
            "expected_status": expected_status,
            "predicted_invoice_id": pred_inv,
            "predicted_status": pred_status,
            "correct": correct,
            "notes": gt.get("notes"),
        })

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    total = len(ground_truth) or 1
    accuracy = sum(1 for d in details if d["correct"]) / total

    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "details": details,
    }
