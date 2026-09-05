#!/usr/bin/env python
"""Kick off the AI Finance Controller CrewAI Flow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CREWAI_TRACING_ENABLED"] = "false"

from src.finance_controller.flow import run_finance_flow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI Finance Controller CrewAI Flow")
    parser.add_argument("--request", default="Run AR reconciliation on demo feeds")
    parser.add_argument("--mode", default="AR", choices=["AP", "AR", "BOTH"])
    parser.add_argument("--bank", default=None)
    parser.add_argument("--processor", default=None)
    parser.add_argument("--ground-truth", default=None)
    args = parser.parse_args()

    result = run_finance_flow(
        user_request=args.request,
        ar_ap_mode=args.mode,
        bank_csv_path=args.bank,
        payment_processor_csv_path=args.processor,
        ground_truth_csv_path=args.ground_truth,
    )
    print(json.dumps({
        "run_id": result["run_id"],
        "evaluation_metrics": result["evaluation_metrics"],
        "persistence_summary": result["persistence_summary"],
        "journal_entries_summary": result["journal_entries_summary"],
    }, indent=2))
    print("\n--- report preview ---\n")
    print(result["final_report"][:1200])


if __name__ == "__main__":
    main()
