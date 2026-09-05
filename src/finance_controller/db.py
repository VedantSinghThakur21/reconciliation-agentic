from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.finance_controller.schema import SCHEMA_SQL

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "finance_controller.db"

_OPERATIONAL_TABLES = (
    "source_transactions",
    "match_candidates",
    "exceptions",
    "human_reviews",
    "reconciliation_results",
    "journal_entries",
    "erp_writeback_log",
    "evaluation_metrics",
    "audit_log",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


class FinanceDB:
    """SQLite persistence for the AI Finance Controller — all operational rows scoped by run_id."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    def _migrate(self) -> None:
        """Add run_id to legacy tables without wiping historical flow_runs."""
        # flow_runs.bank_pdf_path
        if "flow_runs" in self._table_names() and "bank_pdf_path" not in self._columns("flow_runs"):
            self.conn.execute("ALTER TABLE flow_runs ADD COLUMN bank_pdf_path TEXT")

        latest = self.conn.execute(
            "SELECT id FROM flow_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        fallback_run = latest["id"] if latest else None

        for table in _OPERATIONAL_TABLES:
            if table not in self._table_names():
                continue
            cols = self._columns(table)
            if "run_id" not in cols:
                # SQLite cannot add NOT NULL without default on existing rows
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN run_id TEXT")
                if fallback_run:
                    self.conn.execute(
                        f"UPDATE {table} SET run_id = ? WHERE run_id IS NULL OR run_id = ''",
                        (fallback_run,),
                    )

        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_src_run ON source_transactions(run_id);
            CREATE INDEX IF NOT EXISTS idx_cand_run ON match_candidates(run_id);
            CREATE INDEX IF NOT EXISTS idx_exc_run ON exceptions(run_id);
            CREATE INDEX IF NOT EXISTS idx_rev_run ON human_reviews(run_id);
            CREATE INDEX IF NOT EXISTS idx_results_run ON reconciliation_results(run_id);
            CREATE INDEX IF NOT EXISTS idx_journals_run ON journal_entries(run_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_run ON evaluation_metrics(run_id);
            CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
            """
        )

    def _table_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {str(r["name"]) for r in rows}

    def reset_operational_tables(self) -> None:
        """
        Deprecated no-op.

        Prior versions wiped all operational rows on every run. Isolation requires
        preserving prior run_id data — new runs always create a fresh run_id instead.
        """
        return

    def clear_run_operational(self, run_id: str) -> None:
        """Clear operational rows for a single run (retry only — never other runs)."""
        for t in _OPERATIONAL_TABLES:
            if t not in self._table_names():
                continue
            if "run_id" in self._columns(t):
                self.conn.execute(f"DELETE FROM {t} WHERE run_id = ?", (run_id,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def audit(
        self,
        event_type: str,
        message: str,
        entity_id: str | None = None,
        payload: Any = None,
        *,
        run_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO audit_log (run_id, event_type, entity_id, message, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                event_type,
                entity_id,
                message,
                _json(payload) if payload is not None else None,
                _now(),
            ),
        )
        self.conn.commit()

    def create_run(
        self,
        user_request: str,
        ar_ap_mode: str,
        *,
        bank_pdf_path: str | None = None,
    ) -> str:
        run_id = f"FLOW-{uuid.uuid4().hex[:10].upper()}"
        self.conn.execute(
            """INSERT INTO flow_runs
               (id, user_request, ar_ap_mode, status, stages_json, bank_pdf_path, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, user_request, ar_ap_mode, "running", _json([]), bank_pdf_path, _now()),
        )
        self.conn.commit()
        self.audit(
            "run_started",
            f"Flow started ({ar_ap_mode})",
            entity_id=run_id,
            run_id=run_id,
            payload={"bank_pdf_path": bank_pdf_path},
        )
        return run_id

    def update_stages(self, run_id: str, stages: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "UPDATE flow_runs SET stages_json = ? WHERE id = ?",
            (_json(stages), run_id),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: str,
        summary: dict[str, Any],
        report: str,
        status: str = "completed",
    ) -> None:
        self.conn.execute(
            """UPDATE flow_runs
               SET status = ?, summary_json = ?, final_report = ?, finished_at = ?
               WHERE id = ?""",
            (status, _json(summary), report, _now(), run_id),
        )
        self.conn.commit()
        self.audit(
            "run_finished",
            f"Flow {status}",
            entity_id=run_id,
            run_id=run_id,
            payload=summary,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM flow_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["stages"] = json.loads(d.pop("stages_json") or "[]")
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        return d

    def latest_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id FROM flow_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return self.get_run(row["id"])

    def latest_dashboard_run(self) -> dict[str, Any] | None:
        """Newest completed run with usable workspace data when possible."""
        rows = self.conn.execute(
            """SELECT id FROM flow_runs
               WHERE status = 'completed'
               ORDER BY COALESCE(finished_at, started_at) DESC
               LIMIT 20"""
        ).fetchall()
        if not rows:
            return self.latest_run()

        # Prefer a completed run that actually has ERP/bank feed rows hydrated.
        for row in rows:
            rid = row["id"]
            has_feed = self.conn.execute(
                """SELECT 1 FROM source_transactions
                   WHERE run_id = ? AND source IN ('erp', 'bank', 'payment_processor')
                   LIMIT 1""",
                (rid,),
            ).fetchone()
            if has_feed:
                return self.get_run(rid)

        return self.get_run(rows[0]["id"])
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, user_request, ar_ap_mode, status, started_at, finished_at,
                      bank_pdf_path, summary_json
               FROM flow_runs
               ORDER BY started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            summary = json.loads(d.pop("summary_json") or "{}")
            ev = summary.get("evaluation") or summary.get("metrics") or {}
            d["summary_preview"] = {
                "match_rate": ev.get("match_rate"),
                "accuracy": ev.get("accuracy"),
                "f1": ev.get("f1"),
                "exception_count": summary.get("exception_count"),
                "auto_match_count": summary.get("auto_match_count"),
            }
            out.append(d)
        return out

    def insert_sources(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        for r in records:
            self.conn.execute(
                """INSERT OR REPLACE INTO source_transactions
                   (id, run_id, source, transaction_id, txn_date, amount, currency, merchant, reference, description,
                    merchant_norm, date_norm, amount_norm, currency_norm, reference_norm, raw_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["source"],
                    r["transaction_id"],
                    r.get("txn_date"),
                    r.get("amount"),
                    r.get("currency"),
                    r.get("merchant"),
                    r.get("reference"),
                    r.get("description"),
                    r.get("merchant_norm"),
                    r.get("date_norm"),
                    r.get("amount_norm"),
                    r.get("currency_norm"),
                    r.get("reference_norm"),
                    _json(r.get("raw") or r),
                    _now(),
                ),
            )
        self.conn.commit()

    def update_normalization(self, txn_db_id: str, fields: dict[str, Any]) -> None:
        self.conn.execute(
            """UPDATE source_transactions
               SET merchant_norm=?, date_norm=?, amount_norm=?, currency_norm=?, reference_norm=?
               WHERE id=?""",
            (
                fields.get("merchant_norm"),
                fields.get("date_norm"),
                fields.get("amount_norm"),
                fields.get("currency_norm"),
                fields.get("reference_norm"),
                txn_db_id,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def fetch_sources(self, run_id: str, source: str | None = None) -> list[dict[str, Any]]:
        if source:
            rows = self.conn.execute(
                """SELECT * FROM source_transactions
                   WHERE run_id = ? AND source = ?
                   ORDER BY date_norm, transaction_id""",
                (run_id, source),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM source_transactions
                   WHERE run_id = ?
                   ORDER BY source, date_norm, transaction_id""",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_candidates(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO match_candidates
                   (id, run_id, erp_txn_id, counter_txn_id, strategy, amount_score, merchant_score, date_score,
                    reference_score, confidence, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["erp_txn_id"],
                    r["counter_txn_id"],
                    r["strategy"],
                    r.get("amount_score"),
                    r.get("merchant_score"),
                    r.get("date_score"),
                    r.get("reference_score"),
                    r.get("confidence"),
                    _json(r.get("evidence") or {}),
                    _now(),
                ),
            )
        self.conn.commit()

    def clear_candidates(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM match_candidates WHERE run_id = ?", (run_id,))
        self.conn.commit()

    def fetch_candidates(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM match_candidates WHERE run_id = ? ORDER BY confidence DESC",
                (run_id,),
            ).fetchall()
        ]

    def insert_exceptions(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO exceptions
                   (id, run_id, transaction_id, exception_type, recommended_action, confidence, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["transaction_id"],
                    r["exception_type"],
                    r.get("recommended_action"),
                    r.get("confidence"),
                    _json(r.get("details") or {}),
                    _now(),
                ),
            )
        self.conn.commit()

    def fetch_exceptions(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM exceptions WHERE run_id = ? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()
        ]

    def get_exception(self, exception_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_reviews(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO human_reviews
                   (id, run_id, transaction_id, decision, reason, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["transaction_id"],
                    r["decision"],
                    r.get("reason"),
                    r.get("confidence"),
                    _now(),
                ),
            )
        self.conn.commit()

    def fetch_reviews(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM human_reviews WHERE run_id = ? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()
        ]

    def apply_human_decision(
        self,
        *,
        run_id: str,
        transaction_id: str,
        decision: str,
        reason: str,
        confidence: float,
        review_id: str,
    ) -> dict[str, Any]:
        """Persist a real human decision and update matching reconciliation results.

        Returns the review row plus `created` (False when a prior decision exists).
        """
        from src.finance_controller.review_state import DECISION_TO_RESULT_STATUS

        existing = next(
            (r for r in self.fetch_reviews(run_id) if r["transaction_id"] == transaction_id),
            None,
        )
        if existing:
            return {**existing, "created": False}

        self.insert_reviews(
            [
                {
                    "id": review_id,
                    "transaction_id": transaction_id,
                    "decision": decision,
                    "reason": reason,
                    "confidence": confidence,
                }
            ],
            run_id=run_id,
        )
        final_status = DECISION_TO_RESULT_STATUS.get(decision, "HUMAN_REVIEW")
        self.conn.execute(
            """UPDATE reconciliation_results
               SET final_status = ?
               WHERE run_id = ?
                 AND (transaction_id = ? OR matched_txn_id = ?)
                 AND final_status IN ('HUMAN_REVIEW', 'PENDING', 'UNMATCHED')""",
            (final_status, run_id, transaction_id, transaction_id),
        )
        self.conn.commit()
        row = next(
            (r for r in self.fetch_reviews(run_id) if r["transaction_id"] == transaction_id),
            None,
        )
        out = dict(
            row
            or {
                "id": review_id,
                "transaction_id": transaction_id,
                "decision": decision,
                "reason": reason,
                "confidence": confidence,
            }
        )
        out["created"] = True
        return out

    def insert_results(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO reconciliation_results
                   (id, run_id, transaction_id, matched_txn_id, final_status, confidence,
                    reconciled_amount, strategy, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["transaction_id"],
                    r.get("matched_txn_id"),
                    r["final_status"],
                    r.get("confidence"),
                    r.get("reconciled_amount"),
                    r.get("strategy"),
                    _json(r.get("details") or {}),
                    _now(),
                ),
            )
        self.conn.commit()

    def fetch_results(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM reconciliation_results WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        ]

    def insert_journals(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO journal_entries
                   (id, run_id, entry_type, status, debit_account, credit_account, amount,
                    currency, transaction_id, memo, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"],
                    run_id,
                    r["entry_type"],
                    r["status"],
                    r["debit_account"],
                    r["credit_account"],
                    r["amount"],
                    r.get("currency") or "INR",
                    r.get("transaction_id"),
                    r.get("memo"),
                    _now(),
                ),
            )
        self.conn.commit()

    def insert_writebacks(self, rows: list[dict[str, Any]], *, run_id: str) -> None:
        for r in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO erp_writeback_log
                   (id, run_id, journal_id, status, message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["id"], run_id, r["journal_id"], r["status"], r.get("message"), _now()),
            )
        self.conn.commit()

    def fetch_journals(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM journal_entries WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        ]

    def fetch_writebacks(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM erp_writeback_log WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        ]

    def save_metrics(self, metrics: dict[str, Any], *, run_id: str) -> None:
        self.conn.execute(
            """INSERT INTO evaluation_metrics
               (run_id, accuracy, precision_score, recall, f1, match_rate, false_match_rate,
                tp, fp, fn, tn, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                metrics.get("accuracy"),
                metrics.get("precision"),
                metrics.get("recall"),
                metrics.get("f1"),
                metrics.get("match_rate"),
                metrics.get("false_match_rate"),
                metrics.get("tp"),
                metrics.get("fp"),
                metrics.get("fn"),
                metrics.get("tn"),
                _json(metrics.get("details") or {}),
                _now(),
            ),
        )
        self.conn.commit()

    def get_metrics(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM evaluation_metrics
               WHERE run_id = ?
               ORDER BY id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["precision"] = d.pop("precision_score")
        d["details"] = json.loads(d.pop("details_json") or "{}")
        return d

    def latest_metrics(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id:
            return self.get_metrics(run_id)
        run = self.latest_dashboard_run()
        if not run:
            return None
        return self.get_metrics(run["id"])

    def fetch_audit(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self.conn.execute(
                """SELECT * FROM audit_log
                   WHERE run_id = ? OR entity_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (run_id, run_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "null")
            out.append(d)
        return out
