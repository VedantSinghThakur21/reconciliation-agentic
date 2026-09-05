from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["CREWAI_TRACING_ENABLED"] = "false"

from src.finance_controller.flow import run_finance_flow


def test_crewai_finance_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "finance_controller.db"
    result = run_finance_flow(
        user_request="Run AR reconciliation on demo feeds",
        ar_ap_mode="AR",
        db_path=db_path,
    )
    assert result["run_id"].startswith("FLOW-")
    assert len(result["stages"]) >= 10
    assert result["evaluation_metrics"]["accuracy"] is not None
    assert result["persistence_summary"]["total_results"] >= 1
    assert result["journal_entries_summary"]["total_entries_created"] >= 1
    assert "Executive Summary" in result["final_report"]
