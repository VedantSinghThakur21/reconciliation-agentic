from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.db.schema import SCHEMA_SQL

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "reconq.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


class Database:
    """SQLite persistence for the AI Finance Controller prototype."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── runs ──────────────────────────────────────────────────────────

    def create_run(self, *, mode: str = "demo", use_ai: bool = False) -> str:
        run_id = f"RUN-{uuid.uuid4().hex[:10].upper()}"
        self._conn.execute(
            "INSERT INTO runs (id, started_at, status, mode, use_ai, stages_json) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _now(), "running", mode, int(use_ai), _json([])),
        )
        self._conn.commit()
        self.log_audit(run_id, "run_started", message=f"Reconciliation run started (mode={mode})")
        return run_id

    def update_stages(self, run_id: str, stages: list[dict[str, Any]]) -> None:
        self._conn.execute(
            "UPDATE runs SET stages_json = ? WHERE id = ?",
            (_json(stages), run_id),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, summary: dict[str, Any], status: str = "completed") -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE id = ?",
            (_now(), status, _json(summary), run_id),
        )
        self._conn.commit()
        self.log_audit(run_id, "run_finished", message=f"Run {status}", payload=summary)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def latest_run(self) -> dict[str, Any] | None:
        rows = self.list_runs(1)
        return rows[0] if rows else None

    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        d["stages"] = json.loads(d.pop("stages_json") or "[]")
        d["use_ai"] = bool(d["use_ai"])
        return d

    # ── transactions ──────────────────────────────────────────────────

    def save_transactions(self, run_id: str, items: list[dict[str, Any]]) -> None:
        for item in items:
            self._conn.execute(
                """INSERT OR REPLACE INTO transactions
                   (id, run_id, source, kind, external_id, party_name, amount, currency, txn_date, reference, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.get("id") or f"TX-{uuid.uuid4().hex[:8]}",
                    run_id,
                    item["source"],
                    item["kind"],
                    item["external_id"],
                    item.get("party_name"),
                    float(item["amount"]),
                    item.get("currency") or "INR",
                    item.get("txn_date"),
                    item.get("reference"),
                    _json(item.get("raw") or item),
                ),
            )
        self._conn.commit()

    def list_transactions(self, run_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM transactions WHERE run_id = ? AND kind = ? ORDER BY txn_date",
                (run_id, kind),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM transactions WHERE run_id = ? ORDER BY kind, txn_date",
                (run_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = json.loads(d.pop("raw_json") or "{}")
            out.append(d)
        return out

    # ── results ───────────────────────────────────────────────────────

    def save_results(self, run_id: str, decisions: list[dict[str, Any]]) -> None:
        for d in decisions:
            rid = f"RES-{uuid.uuid4().hex[:8].upper()}"
            self._conn.execute(
                """INSERT INTO results
                   (id, run_id, payment_id, invoice_id, status, confidence, tier,
                    amount_applied, discrepancy, treatment, reasoning, source, review_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rid,
                    run_id,
                    d.get("paymentId"),
                    d.get("invoiceId"),
                    d.get("status") or "unknown",
                    float(d.get("confidence") or 0),
                    d.get("tier"),
                    float(d.get("amountApplied") or 0),
                    float(d.get("discrepancyAmount") or 0),
                    d.get("accountingTreatment"),
                    d.get("reasoning"),
                    d.get("source"),
                    d.get("reviewReason"),
                ),
            )
        self._conn.commit()

    def list_results(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM results WHERE run_id = ? ORDER BY confidence DESC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── exceptions ────────────────────────────────────────────────────

    def save_exceptions(self, run_id: str, items: list[dict[str, Any]]) -> None:
        for item in items:
            self._conn.execute(
                """INSERT OR REPLACE INTO exceptions
                   (id, run_id, payment_id, invoice_id, confidence, reason, reasoning,
                    amount_applied, status, created_at, decided_at, decided_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["id"],
                    run_id,
                    item["payment_id"],
                    item.get("invoice_id"),
                    float(item.get("confidence") or 0),
                    item.get("reason"),
                    item.get("reasoning"),
                    float(item.get("amount_applied") or 0),
                    item.get("status") or "pending",
                    item.get("created_at") or _now(),
                    item.get("decided_at"),
                    item.get("decided_by"),
                ),
            )
        self._conn.commit()

    def list_exceptions(self, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM exceptions WHERE 1=1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_exception(self, exception_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
        ).fetchone()
        return dict(row) if row else None

    def resolve_exception(
        self,
        exception_id: str,
        *,
        decision: str,
        decided_by: str = "analyst",
        note: str | None = None,
    ) -> dict[str, Any]:
        item = self.get_exception(exception_id)
        if not item:
            raise KeyError(f"Unknown exception: {exception_id}")
        if decision not in ("confirmed", "rejected", "resolved"):
            raise ValueError(f"Invalid decision: {decision}")
        decided_at = _now()
        self._conn.execute(
            "UPDATE exceptions SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (decision, decided_at, decided_by, exception_id),
        )
        self._conn.commit()
        self.log_audit(
            item["run_id"],
            "exception_resolved",
            entity_type="exception",
            entity_id=exception_id,
            message=f"Exception {exception_id} marked {decision}" + (f": {note}" if note else ""),
            payload={"decision": decision, "decided_by": decided_by, "note": note},
        )
        return self.get_exception(exception_id)  # type: ignore[return-value]

    # ── metrics ───────────────────────────────────────────────────────

    def save_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO metrics
               (run_id, accuracy, precision_score, recall, f1, match_rate,
                auto_reconciled, pending_review, total_payments, unreconciled_amount, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                metrics.get("accuracy"),
                metrics.get("precision"),
                metrics.get("recall"),
                metrics.get("f1"),
                metrics.get("match_rate"),
                metrics.get("auto_reconciled"),
                metrics.get("pending_review"),
                metrics.get("total_payments"),
                metrics.get("unreconciled_amount"),
                _json(metrics.get("details") or {}),
            ),
        )
        self._conn.commit()

    def get_metrics(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM metrics WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        d["precision"] = d.pop("precision_score")
        return d

    # ── audit ─────────────────────────────────────────────────────────

    def log_audit(
        self,
        run_id: str | None,
        event_type: str,
        *,
        message: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: Any = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO audit_log
               (run_id, event_type, entity_type, entity_id, message, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, event_type, entity_type, entity_id, message, _json(payload) if payload is not None else None, _now()),
        )
        self._conn.commit()

    def list_audit(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "null")
            out.append(d)
        return out
