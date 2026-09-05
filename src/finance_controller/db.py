from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.finance_controller.schema import SCHEMA_SQL

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "finance_controller.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


class FinanceDB:
    """SQLite persistence for the AI Finance Controller CrewAI Flow."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def reset_operational_tables(self) -> None:
        tables = [
            "erp_writeback_log",
            "journal_entries",
            "evaluation_metrics",
            "reconciliation_results",
            "human_reviews",
            "exceptions",
            "match_candidates",
            "source_transactions",
            "audit_log",
        ]
        for t in tables:
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def audit(self, event_type: str, message: str, entity_id: str | None = None, payload: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (event_type, entity_id, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_id, message, _json(payload) if payload is not None else None, _now()),
        )
        self.conn.commit()

    def create_run(self, user_request: str, ar_ap_mode: str) -> str:
        run_id = f"FLOW-{uuid.uuid4().hex[:10].upper()}"
        self.conn.execute(
            "INSERT INTO flow_runs (id, user_request, ar_ap_mode, status, stages_json, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, user_request, ar_ap_mode, "running", _json([]), _now()),
        )
        self.conn.commit()
        self.audit("run_started", f"Flow started ({ar_ap_mode})", entity_id=run_id)
        return run_id

    def update_stages(self, run_id: str, stages: list[dict[str, Any]]) -> None:
        self.conn.execute("UPDATE flow_runs SET stages_json = ? WHERE id = ?", (_json(stages), run_id))
        self.conn.commit()

    def finish_run(self, run_id: str, summary: dict[str, Any], report: str, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE flow_runs SET status = ?, summary_json = ?, final_report = ?, finished_at = ? WHERE id = ?",
            (status, _json(summary), report, _now(), run_id),
        )
        self.conn.commit()
        self.audit("run_finished", f"Flow {status}", entity_id=run_id, payload=summary)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM flow_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["stages"] = json.loads(d.pop("stages_json") or "[]")
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        return d

    def latest_run(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM flow_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        return self.get_run(row["id"])

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, user_request, ar_ap_mode, status, started_at, finished_at FROM flow_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_sources(self, records: list[dict[str, Any]]) -> None:
        for r in records:
            self.conn.execute(
                """INSERT OR REPLACE INTO source_transactions
                   (id, source, transaction_id, txn_date, amount, currency, merchant, reference, description,
                    merchant_norm, date_norm, amount_norm, currency_norm, reference_norm, raw_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["source"], r["transaction_id"], r.get("txn_date"), r.get("amount"),
                    r.get("currency"), r.get("merchant"), r.get("reference"), r.get("description"),
                    r.get("merchant_norm"), r.get("date_norm"), r.get("amount_norm"),
                    r.get("currency_norm"), r.get("reference_norm"), _json(r.get("raw") or r), _now(),
                ),
            )
        self.conn.commit()

    def update_normalization(self, txn_db_id: str, fields: dict[str, Any]) -> None:
        self.conn.execute(
            """UPDATE source_transactions SET merchant_norm=?, date_norm=?, amount_norm=?, currency_norm=?, reference_norm=?
               WHERE id=?""",
            (
                fields.get("merchant_norm"), fields.get("date_norm"), fields.get("amount_norm"),
                fields.get("currency_norm"), fields.get("reference_norm"), txn_db_id,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def fetch_sources(self, source: str | None = None) -> list[dict[str, Any]]:
        if source:
            rows = self.conn.execute(
                "SELECT * FROM source_transactions WHERE source = ? ORDER BY date_norm, transaction_id",
                (source,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM source_transactions ORDER BY source, date_norm, transaction_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_candidates(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO match_candidates
                   (id, erp_txn_id, counter_txn_id, strategy, amount_score, merchant_score, date_score,
                    reference_score, confidence, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["erp_txn_id"], r["counter_txn_id"], r["strategy"],
                    r.get("amount_score"), r.get("merchant_score"), r.get("date_score"),
                    r.get("reference_score"), r.get("confidence"), _json(r.get("evidence") or {}), _now(),
                ),
            )
        self.conn.commit()

    def fetch_candidates(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM match_candidates ORDER BY confidence DESC"
        ).fetchall()]

    def insert_exceptions(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO exceptions
                   (id, transaction_id, exception_type, recommended_action, confidence, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["transaction_id"], r["exception_type"], r.get("recommended_action"),
                    r.get("confidence"), _json(r.get("details") or {}), _now(),
                ),
            )
        self.conn.commit()

    def fetch_exceptions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM exceptions ORDER BY created_at DESC").fetchall()]

    def insert_reviews(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO human_reviews
                   (id, transaction_id, decision, reason, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["id"], r["transaction_id"], r["decision"], r.get("reason"), r.get("confidence"), _now()),
            )
        self.conn.commit()

    def fetch_reviews(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM human_reviews ORDER BY created_at DESC").fetchall()]

    def insert_results(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO reconciliation_results
                   (id, transaction_id, matched_txn_id, final_status, confidence, reconciled_amount, strategy, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["transaction_id"], r.get("matched_txn_id"), r["final_status"],
                    r.get("confidence"), r.get("reconciled_amount"), r.get("strategy"),
                    _json(r.get("details") or {}), _now(),
                ),
            )
        self.conn.commit()

    def fetch_results(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM reconciliation_results ORDER BY created_at"
        ).fetchall()]

    def insert_journals(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO journal_entries
                   (id, entry_type, status, debit_account, credit_account, amount, currency, transaction_id, memo, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["entry_type"], r["status"], r["debit_account"], r["credit_account"],
                    r["amount"], r.get("currency") or "INR", r.get("transaction_id"), r.get("memo"), _now(),
                ),
            )
        self.conn.commit()

    def insert_writebacks(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO erp_writeback_log (id, journal_id, status, message, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["id"], r["journal_id"], r["status"], r.get("message"), _now()),
            )
        self.conn.commit()

    def fetch_journals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM journal_entries ORDER BY created_at").fetchall()]

    def fetch_writebacks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM erp_writeback_log ORDER BY created_at").fetchall()]

    def save_metrics(self, metrics: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO evaluation_metrics
               (accuracy, precision_score, recall, f1, match_rate, false_match_rate, tp, fp, fn, tn, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metrics.get("accuracy"), metrics.get("precision"), metrics.get("recall"), metrics.get("f1"),
                metrics.get("match_rate"), metrics.get("false_match_rate"),
                metrics.get("tp"), metrics.get("fp"), metrics.get("fn"), metrics.get("tn"),
                _json(metrics.get("details") or {}), _now(),
            ),
        )
        self.conn.commit()

    def latest_metrics(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["precision"] = d.pop("precision_score")
        d["details"] = json.loads(d.pop("details_json") or "{}")
        return d

    def fetch_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "null")
            out.append(d)
        return out
