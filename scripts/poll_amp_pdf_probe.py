#!/usr/bin/env python3
"""Poll a known AMP kickoff and dump PDF-extractor evidence."""
from __future__ import annotations

import json
import os
import re
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

kid = sys.argv[1] if len(sys.argv) > 1 else "b04f7315-bacd-449a-a6df-7ec109a9782a"
c = CrewAIClient()
print(f"polling {kid}", flush=True)
try:
    body = c.wait(kid, max_wait_sec=600)
except Exception as e:  # noqa: BLE001
    print("WAIT_ERROR", type(e).__name__, e)
    http, _raw, body = c.status(kid)
    print("last_http", http)
    print(json.dumps(body, indent=2, default=str)[:8000])
    raise SystemExit(1)

out = ROOT / "data" / "uploads" / "_amp_pdf_probe_result.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
print("state", body.get("state") or body.get("status"))
print("wrote", out)

text = json.dumps(body, default=str)
for pat in [
    "Bank Statement PDF",
    "records_extracted",
    "extract_bank_pdf",
    "No PDF",
    "skipped",
    "bank_pdf",
]:
    idxs = list(re.finditer(re.escape(pat), text, re.I))
    print(f"matches[{pat}]={len(idxs)}")

res = body.get("result") or body.get("result_json") or body.get("output")
if isinstance(res, (dict, list)):
    print("RESULT_JSON")
    print(json.dumps(res, indent=2, default=str)[:12000])
else:
    print("RESULT_TEXT")
    print(str(res)[:12000])
