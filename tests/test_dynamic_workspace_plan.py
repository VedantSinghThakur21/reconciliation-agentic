"""Unit tests for dynamic workspace planning (mocked LLM — no keyword maps)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.agui.builders import compose_workspace
from src.agui.planner import plan_workspace
from src.finance_controller.db import FinanceDB
from src.finance_controller.flow import run_finance_flow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "plan.db"
    run_finance_flow(
        user_request="seed",
        ar_ap_mode="AR",
        bank_csv_path=str(ROOT / "data" / "demo" / "bank.csv"),
        payment_processor_csv_path=str(ROOT / "data" / "demo" / "payment_processor.csv"),
        ground_truth_csv_path=str(ROOT / "data" / "demo" / "ground_truth.csv"),
        db_path=db_path,
    )
    return FinanceDB(db_path)


def _mock_plan(primary: str, secondary: list[str], components: list[str], **extra):
    return {
        "intent": {"primary": primary, "secondary": secondary},
        "reasoning": f"mocked semantic plan for {primary}",
        "confidence": 0.91,
        "actions": {
            "run_reconciliation": extra.get("run_reconciliation", False),
            "needs_clarification": False,
            "clarification_question": None,
        },
        "txn_ref": extra.get("txn_ref"),
        "ui": {
            "title": extra.get("title", primary.replace("_", " ").title()),
            "layout": extra.get("layout", "focused"),
            "components": components,
        },
    }


@patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}, clear=False)
@patch("src.agui.planner._call_gemini")
@patch("src.agui.planner._call_groq")
def test_paraphrase_attention_uses_groq(mock_groq, mock_gemini, seeded_db):
    mock_groq.return_value = _mock_plan(
        "exception_review",
        [],
        ["kpi", "exception_table", "recommendation"],
        title="Items needing eyes",
    )
    plan = plan_workspace("What's still sitting on the review pile?")
    assert plan.source == "groq"
    mock_gemini.assert_not_called()
    ws = compose_workspace(seeded_db, plan, seeded_db.latest_dashboard_run()["id"])
    assert ws.status == "ok"
    assert "exception_table" in [c.type for c in ws.components]


@patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}, clear=False)
@patch("src.agui.planner._call_gemini")
@patch("src.agui.planner._call_groq")
def test_paraphrase_reconcile_with_secondary_attention(mock_groq, mock_gemini, seeded_db):
    mock_groq.return_value = _mock_plan(
        "reconciliation",
        ["exception_review"],
        ["metric_grid", "chart", "kpi", "exception_table", "recommendation"],
        run_reconciliation=True,
        title="Close the books + attention",
        layout="dashboard",
    )
    plan = plan_workspace("Could you close out today's books and flag anything I should look at?")
    assert plan.source == "groq"
    assert plan.actions.run_reconciliation is True
    mock_gemini.assert_not_called()
    ws = compose_workspace(seeded_db, plan, seeded_db.latest_dashboard_run()["id"])
    assert ws.status == "ok"
    types = [c.type for c in ws.components]
    assert "exception_table" in types


@patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}, clear=False)
@patch("src.agui.planner._call_gemini")
@patch("src.agui.planner._call_groq")
def test_paraphrase_amount_mismatch(mock_groq, mock_gemini, seeded_db):
    mock_groq.return_value = _mock_plan(
        "amount_mismatch",
        [],
        ["kpi", "transaction_table", "comparison"],
        title="Where money doesn't match",
    )
    plan = plan_workspace("Pull the payments where the amounts don't line up.")
    assert plan.source == "groq"
    mock_gemini.assert_not_called()
    ws = compose_workspace(seeded_db, plan, seeded_db.latest_dashboard_run()["id"])
    assert ws.status == "ok"


@patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}, clear=False)
@patch("src.agui.planner._call_gemini")
@patch("src.agui.planner._call_groq")
def test_no_fabricated_txn(mock_groq, mock_gemini, seeded_db):
    mock_groq.return_value = _mock_plan(
        "transaction_investigation",
        [],
        ["transaction_detail", "comparison", "metric_grid", "recommendation"],
        txn_ref="TXN-0047",
        title="Why TXN-0047",
    )
    plan = plan_workspace("Why did TXN_0047 fail?")
    assert plan.source == "groq"
    mock_gemini.assert_not_called()
    ws = compose_workspace(seeded_db, plan, seeded_db.latest_dashboard_run()["id"])
    assert ws.status == "unavailable"


def test_classify_module_has_no_phrase_table():
    src = (ROOT / "src" / "assistant" / "classify.py").read_text(encoding="utf-8")
    assert "needs my attention" not in src
    assert "amount mismatch" not in src
    assert "_heuristic_classify" not in src
    planner = (ROOT / "src" / "agui" / "planner.py").read_text(encoding="utf-8")
    assert "needs my attention" not in planner
    assert "stuck on my plate" not in planner
    assert "_call_groq" in planner
    assert "GROQ_API_KEY" in planner
