from __future__ import annotations

from pathlib import Path

from src.agents.orchestrator import FinanceControllerOrchestrator, answer_finance_question
from src.db.store import Database


def test_orchestrator_demo_run(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    orch = FinanceControllerOrchestrator(db)
    result = orch.run(mode="demo", use_ai=True)

    assert result["run_id"].startswith("RUN-")
    assert result["summary"]["total_payments"] >= 50
    assert result["summary"]["auto_reconciled"] >= 1
    assert result["summary"]["throughput_per_sec"] is not None
    assert result["cash_position"]["cash_applied"] >= 0
    assert db.get_metrics(result["run_id"]) is not None
    assert len(db.list_exceptions(result["run_id"])) >= 1

    qa = answer_finance_question(db, "What is the cash position?", result["run_id"])
    assert "cash" in qa["answer"].lower() or "ar" in qa["answer"].lower()
    db.close()
