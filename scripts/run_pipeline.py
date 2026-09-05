#!/usr/bin/env python3
"""CLI harness for reconq-core (no Temporal / no FastAPI)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

from src.pipeline import run_pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reconq-core AR cash-application pipeline")
    parser.add_argument("--invoices", required=True, help="Path to invoice CSV")
    parser.add_argument("--payments", default=None, help="Path to payment CSV (classic path)")
    parser.add_argument(
        "--payments-pdf",
        default=None,
        help="Path to text-layer bank statement PDF (alternative to --payments; no OCR)",
    )
    parser.add_argument("--ai", action="store_true", help="Send leftovers through LangGraph AI matcher")
    parser.add_argument(
        "--use-crewai",
        action="store_true",
        help=(
            "Optional: enrich StrictMatcher leftovers via CrewAI AMP "
            "(requires CREWAI_API_URL + CREWAI_BEARER_TOKEN). Fail-open if unset/unreachable."
        ),
    )
    parser.add_argument("--crewai-bank-csv", default=None, help="Optional bank CSV path for AMP inputs")
    parser.add_argument("--crewai-processor-csv", default=None, help="Optional processor CSV for AMP")
    parser.add_argument("--crewai-ground-truth-csv", default=None, help="Optional ground-truth CSV for AMP")
    parser.add_argument(
        "--crewai-max-wait",
        type=float,
        default=None,
        help="Max seconds to poll AMP status (default 600 / CREWAI_MAX_WAIT_SEC; AMP runs take minutes)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.payments and not args.payments_pdf:
        parser.error("Provide --payments CSV and/or use --payments-pdf for bank statement PDF")
    if args.payments and args.payments_pdf:
        parser.error("Use either --payments or --payments-pdf, not both")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    result = run_pipeline(
        args.invoices,
        args.payments,
        payment_pdf=args.payments_pdf,
        use_ai_graph=args.ai,
        use_crewai=args.use_crewai,
        crewai_bank_csv=args.crewai_bank_csv,
        crewai_processor_csv=args.crewai_processor_csv,
        crewai_ground_truth_csv=args.crewai_ground_truth_csv,
        crewai_max_wait_sec=args.crewai_max_wait,
    )
    summary = result.summary()
    print("=== reconq-core summary ===")
    print(json.dumps(summary, indent=2))
    print("\n=== decisions ===")
    for d in result.decisions:
        print(
            f"  {d.get('paymentId')} -> {d.get('invoiceId')} "
            f"[{d.get('status')}] conf={d.get('confidence')} "
            f"treatment={d.get('accountingTreatment')} source={d.get('source')}"
        )
    print(f"\nHITL queue pending: {len(result.review_queue.pending())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
