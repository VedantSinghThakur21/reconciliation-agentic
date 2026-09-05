"""Pending human-review items must exist as records, flow through AG-UI, and update on action."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["CREWAI_TRACING_ENABLED"] = "false"

from src.agui.builders import build_reconciliation_result, compose_workspace
from src.agui.planner import WorkspacePlan
from src.finance_controller.db import FinanceDB
from src.finance_controller.flow import run_finance_flow
from src.finance_controller.review_state import pending_review_items, reconciliation_summary
from src.finance_controller.utils import new_id

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"


@pytest.fixture(scope="module")
def flow_result(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "pending_agui.db"
    result = run_finance_flow(
        user_request="Run AR reconciliation on demo feeds",
        ar_ap_mode="AR",
        bank_csv_path=str(DEMO / "bank.csv"),
        payment_processor_csv_path=str(DEMO / "payment_processor.csv"),
        ground_truth_csv_path=str(DEMO / "ground_truth.csv"),
        db_path=db_path,
    )
    result["_db_path"] = str(db_path)
    return result


def test_pending_count_matches_actual_item_ids(flow_result):
    db = FinanceDB(flow_result["_db_path"])
    run_id = flow_result["run_id"]
    items = pending_review_items(db, run_id)
    summary = flow_result["human_review_summary"]
    report = flow_result["final_report"]

    count = len(items)
    ids = [i["transaction_id"] for i in items]
    assert summary["pending_human_count"] == count
    assert summary["item_ids"]
    assert len(summary["item_ids"]) == count
    assert "Item IDs: None" not in report
    assert f"Pending Human Count: {count}" in report
    assert "Decisions Applied:" in report
    for tid in ids:
        assert tid in report
        assert i_has_fields(next(i for i in items if i["transaction_id"] == tid))

    results = db.fetch_results(run_id)
    human_review_results = [r for r in results if r["final_status"] == "HUMAN_REVIEW"]
    assert len(human_review_results) >= 1 or count == 0
    if count:
        result_txns = {r["transaction_id"] for r in results}
        for tid in ids:
            assert tid in result_txns or any(
                e["transaction_id"] == tid for e in db.fetch_exceptions(run_id)
            )


def i_has_fields(item: dict) -> bool:
    assert item["human_decision"] is None
    assert item["status"] in {"HUMAN_REVIEW", "PENDING"}
    assert item["id"]
    assert item["transaction_id"]
    assert "amount" in item
    assert "source" in item
    assert "reason" in item
    assert "ai_recommendation" in item
    assert item["ai_recommendation"] != item["human_decision"]
    return True


def test_agui_state_contains_actual_pending_items(flow_result):
    db = FinanceDB(flow_result["_db_path"])
    run_id = flow_result["run_id"]
    items = pending_review_items(db, run_id)
    ui = build_reconciliation_result(db, run_id)
    state = ui.as_state()

    assert state["pending_human_count"] == len(items)
    assert len(state["pending_review_items"]) == len(items)
    assert {r["transaction_id"] for r in state["pending_review_items"]} == {
        r["transaction_id"] for r in items
    }
    data = ui.data or {}
    assert data["pending_human_count"] == len(items)
    assert data["reconciliation_summary"]["pending_human_count"] == len(items)

    types = [c.type for c in ui.components]
    assert "exception_table" in types
    table = next(c for c in ui.components if c.type == "exception_table")
    rows = table.data.get("rows") or []
    assert table.data.get("title") == "Needs Your Attention"
    assert len(rows) == len(items)
    assert all(r.get("human_decision") is None for r in rows)
    assert all(str(r.get("status")) in {"HUMAN_REVIEW", "PENDING"} for r in rows)


def test_planner_omitting_exception_table_still_gets_pending_queue(flow_result):
    db = FinanceDB(flow_result["_db_path"])
    run_id = flow_result["run_id"]
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "reconciliation", "secondary": []},
            "reasoning": "test",
            "confidence": 1,
            "actions": {"run_reconciliation": False},
            "ui": {"title": "Recon", "layout": "dashboard", "components": ["metric_grid"]},
        }
    )
    ui = compose_workspace(db, plan, run_id)
    types = [c.type for c in ui.components]
    assert "exception_table" in types
    assert (ui.data or {}).get("pending_human_count") == len(pending_review_items(db, run_id))


def test_approve_one_item_updates_queue_and_database(tmp_path):
    db_path = tmp_path / "approve_one.db"
    result = run_finance_flow(
        user_request="Run AR reconciliation on demo feeds",
        ar_ap_mode="AR",
        bank_csv_path=str(DEMO / "bank.csv"),
        payment_processor_csv_path=str(DEMO / "payment_processor.csv"),
        ground_truth_csv_path=str(DEMO / "ground_truth.csv"),
        db_path=db_path,
    )
    db = FinanceDB(db_path)
    run_id = result["run_id"]
    before_items = pending_review_items(db, run_id)
    before_summary = reconciliation_summary(db, run_id)
    assert before_items, "demo run should leave items for human review"
    target = before_items[0]

    review = db.apply_human_decision(
        run_id=run_id,
        transaction_id=target["transaction_id"],
        decision="APPROVED",
        reason="Manual UI decision: APPROVED",
        confidence=float(target.get("confidence") or 0),
        review_id=new_id("REV"),
    )
    assert review["created"] is True
    assert review["decision"] == "APPROVED"

    after_items = pending_review_items(db, run_id)
    after_summary = reconciliation_summary(db, run_id)
    assert len(after_items) == len(before_items) - 1
    assert target["transaction_id"] not in {i["transaction_id"] for i in after_items}
    assert after_summary["approved"] == before_summary["approved"] + 1
    assert after_summary["pending_human_count"] == before_summary["pending_human_count"] - 1

    stored = [r for r in db.fetch_reviews(run_id) if r["transaction_id"] == target["transaction_id"]]
    assert len(stored) == 1
    assert stored[0]["decision"] == "APPROVED"

    ui = build_reconciliation_result(db, run_id)
    state = ui.as_state()
    assert state["pending_human_count"] == len(after_items)
    queue_ids = {r["transaction_id"] for r in state["pending_review_items"]}
    assert target["transaction_id"] not in queue_ids
    table = next(c for c in ui.components if c.type == "exception_table")
    rows = table.data.get("rows") or []
    assert target["transaction_id"] not in {r.get("transaction_id") for r in rows}
