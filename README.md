# reconq-core

Extracted **core reconciliation engine** from [ReconQ](https://github.com/) — an agentic AI financial reconciliation platform **in active development**.

This repository is **not** the full SaaS product. It is a public-safe, single-tenant slice focused on **AR cash application** (payment → invoice matching) for hackathon / demo use (e.g. Razorpay AI Buildathon).

## What is included

| Module | Role |
|--------|------|
| `src/ingestion/` | CSV / tabular loaders only |
| `src/rule_engine/` | Deterministic StrictMatcher (T1–T5) + shared name matching |
| `src/ai_matcher/` | LangGraph nodes (adapted) + graph wiring + mock LLM |
| `src/verification/` | Double-entry balance check + in-memory HITL review queue |
| `src/audit/` | Accounting treatment + `build_audit_trail` decision logging |
| `src/pipeline.py` | Standalone CLI/pytest entrypoint (no Temporal) |

## What is excluded (on purpose)

- Billing, multi-tenant auth / RBAC
- Live ERP OAuth connectors and credential encryption (demo adapters only)
- Temporal workflows
- PDF / Docling ingestion
- PO matching, credit-note netting, customer advances, suspense GL posting
- Enterprise activity hash-chain audit
- Real customer data, bank statements, or API secrets

## Future work (present in full ReconQ, not here)

- Multi-entity / legal-entity isolation
- 3-way PO ↔ invoice ↔ payment reconciliation
- Credit notes, advances, and full GL journal posting
- Live Gemini / OpenAI providers (this extract defaults to `LLM_PROVIDER=mock`)
- SaaS tenancy, ERP sync, assistant / chat agent

## Quick start

```bash
cd reconq-core
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -r requirements.txt

# Deterministic pipeline (no LLM network calls)
python scripts/run_pipeline.py \
  --invoices data/synthetic/sample_invoices.csv \
  --payments data/synthetic/sample_payments.csv

# Unit tests
python -m pytest tests/ -v
```

Optional AI graph path (still uses MockProvider offline):

```bash
python scripts/run_pipeline.py \
  --invoices data/synthetic/sample_invoices.csv \
  --payments data/synthetic/sample_payments.csv \
  --ai
```

## AI Finance Controller (CrewAI Flow)

End-to-end CrewAI Flow on ERP + bank + payment processor data, persisted to
`data/finance_controller.db`.

```bash
# Generate demo CSVs (brief formats)
python scripts/generate_fc_demo_data.py

# Run the 13-step flow
python scripts/run_finance_flow.py

# API + UI
python scripts/run_api.py          # http://127.0.0.1:8002
cd ui && npm install && npm run dev  # http://localhost:5173
```

## Optional CrewAI AMP leftover enrichment

Core pipeline never requires CrewAI. To enrich StrictMatcher leftovers:

```
CREWAI_API_URL=https://your-crewai-amp-deployment.example.com
CREWAI_BEARER_TOKEN=your-token-here
```

```bash
python scripts/run_pipeline.py --invoices ... --payments ... --use-crewai
```

Contract: `POST /kickoff` then poll `GET /status/{kickoff_id}`. Fail-open on timeout/errors.
See [ARCHITECTURE.md](./ARCHITECTURE.md).

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md).

## License / provenance

Derived from the ReconQ codebase for demonstration of the core matching engine.
Replace sample names (`Acme Corp`, `Test Vendor Ltd`, …) with your own synthetic batch under `data/synthetic/` — never commit real customer files.
