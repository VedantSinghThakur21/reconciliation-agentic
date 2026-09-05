#!/usr/bin/env python3
"""Publish a bank PDF to a public URL and kick off AMP with bank_pdf_path=URL."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

from src.agents.crewai_client import CrewAIClient  # noqa: E402
from src.ingestion.public_upload import publish_public_url  # noqa: E402

pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "uploads" / "Unified_Bank_Statement.pdf"
if not pdf.is_file():
    raise SystemExit(f"missing pdf: {pdf}")

url = publish_public_url(pdf)
print("public_url", url, flush=True)

client = CrewAIClient()
inputs = {
    "user_request": (
        f"Run AR reconciliation. Bank PDF is at {url}. "
        "Bank CSV at bank.csv, payment processor at payment_processor.csv, "
        "ground truth at ground_truth.csv."
    ),
    "ar_ap_mode": "AR",
    "bank_csv_path": "bank.csv",
    "payment_processor_csv_path": "payment_processor.csv",
    "ground_truth_csv_path": "ground_truth.csv",
    "bank_pdf_path": url,
}
kid = client.kickoff(inputs, meta={"source": "public-url-pdf-demo"})
print("kickoff_id", kid, flush=True)
print("inputs", json.dumps(inputs, indent=2), flush=True)

body = client.wait(kid, max_wait_sec=600)
out = ROOT / "data" / "uploads" / "_amp_url_pdf_result.json"
out.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
print("state", body.get("state") or body.get("status"), flush=True)
print("wrote", out, flush=True)
res = body.get("result") or {}
if isinstance(res, dict):
    print("agent_role", res.get("agent_role"), flush=True)
    raw = str(res.get("raw") or "")[:2000]
    print("result_raw_head", raw, flush=True)
