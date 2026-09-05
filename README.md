# ReconIQ — AI-Powered Financial Reconciliation Engine

ReconIQ is an autonomous reconciliation engine that matches incoming payments to open invoices, combining a deterministic rule engine with AI-based investigation for genuinely ambiguous cases — plus a natural-language copilot layer that lets you query the system in plain English and get back a purpose-built view of the answer.

Built for the Razorpay AI Buildathon, Track 04 — AI Finance Controller.

---

## The problem

Finance teams closing their books manually spend days matching bank payments against ERP invoices — a process complicated by unstructured bank narrations, partial settlements, and payments that don't cleanly map to a single invoice. This is typically a multi-day close cycle for mid-market finance teams. ReconIQ automates this loop end-to-end, while staying honest about what it can and can't resolve on its own.

---

## Results

**Run ID:** `FLOW-A4504F2472`

| Metric | Value |
|---|---|
| **Match rate** | **92.0%** |
| Accuracy | 92.31% |
| Precision | 97.83% |
| Recall | 93.75% |
| F1 | 95.74% |
| False-match rate | 2.17% |
| TP / FP / FN / TN | 45 / 1 / 3 / 3 |

**Batch:** 101 source records (51 ERP + 44 bank + 6 payment processor), 52 ground-truth labels, 50 persisted results.

**Outcome breakdown:** 45 auto-matched, 4 flagged for human review, 1 exception resolved. In plain terms: 45 of 50 transactions were reconciled automatically with no human involvement; the remaining 5 were correctly identified as needing attention rather than silently mismatched.

### Exception breakdown (this run)

| Type | Count |
|---|---|
| MISSING_BANK_RECORD | 3 |
| MISSING_ERP_RECORD | 2 |
| MULTIPLE_CANDIDATES | 2 |
| PARTIAL_SETTLEMENT | 1 |

This is not a cherry-picked example — it's the full batch, exceptions included.

Reproduce this eval batch:

```bash
python scripts/generate_fc_demo_data.py
python scripts/run_finance_flow.py
```

---

## Architecture

```
Ingestion (CSV / tabular; optional text-layer PDF)
        ↓
StrictMatcher — deterministic rule tiers (T1–T5)
  exact reference → amount+name → fuzzy name →
  narration substring → MatchMemory patterns
  Clears the majority of transactions instantly, with zero AI involvement.
        ↓ (unmatched leftovers only)
Optional leftover enrichment (env-gated)
  CrewAI AMP (--use-crewai) and/or LangGraph AI matcher (--ai)
  Scores candidates on amount / merchant / date / reference similarity.
  CrewAI runs asynchronously; typically takes several minutes. Fails open —
  if unreachable or misconfigured, the pipeline continues without it.
        ↓
Verification gate
  Accounting tolerance checks, double-entry balance validation.
        ↓
Audit trail + HITL review queue
  Every decision — automated or human-reviewed — is logged with evidence
  and a confidence score.
```

**Design choice, not a limitation:** ReconIQ deliberately closes **one** finance-ops loop — AR cash application (payment → invoice matching) — rather than spreading across multiple. PO matching, credit-note netting, multi-entity support, and full ledger posting are intentionally out of scope for this submission, in favor of depth and measured accuracy on the core loop.

### Copilot layer

On top of the core engine, a natural-language interface lets you query the system directly:

```
User types free text
        ↓
Intent classifier (Gemini when GEMINI_API_KEY is set; OpenAI fallback)
  → classifies into: attention / exceptions / high_confidence /
    reconcile / investigate / approve / reject
  → extracts entities (e.g. transaction reference) when relevant
        ↓
Handler dispatch — joins live run data for that intent
        ↓
Response envelope: { intent, response_type, data }
        ↓
Dynamic UI — purpose-built views by response_type
  (exception summary / table, high-confidence table, reconcile summary,
   investigation detail with per-criterion scores, confirmation card, hint)
```

Workspace AG-UI planning (separate from `/api/assistant/classify`) uses an LLM planner configured via `.env` — primary path is Groq (`GROQ_API_KEY`), with Gemini/OpenAI as optional fallbacks.

Example: typing **"dig into BNK-2101"** is classified as an `investigate` intent, the reference is extracted, the system joins real candidate/exception data, and renders a breakdown of amount/merchant/date/reference match scores with an overall confidence — not a generic chat response.

Approve/reject actions (e.g. "greenlight BNK-2101") call `POST /api/exceptions/{id}/resolve` and write an actual review decision — this records a human-in-the-loop decision; it does not currently post to the ledger or trigger journal entries.

### Live progress streaming (AG-UI)

The CrewAI / AMP path can stream live progress events to the UI using the [AG-UI protocol](https://github.com/ag-ui-protocol/ag-ui), so a multi-minute enrichment run can show progress instead of a static wait. This is additive — if the stream is unavailable, the system falls back to polling (`/api/amp/status/{id}`).

### PDF ingestion (text-layer only)

Bank statement PDFs can be ingested via text-layer extraction (`pdfplumber`), including `scripts/run_pipeline.py --payments-pdf …`. **OCR is not implemented** — scanned/image-only PDFs are not supported. This is an explicit limitation, not a silent gap.

---

## What's out of scope (honestly)

- PO matching, credit-note netting, customer advances, multi-entity support
- Full ledger/journal posting on approve (approve today records a review decision only)
- OCR for scanned PDF bank statements
- GST/TDS-specific deduction handling — narration/fuzzy matching handles general text variation, but there is no dedicated tax-deduction reconciliation logic in this submission

---

## Tech stack

- Python, FastAPI
- CrewAI (multi-agent investigation / AMP)
- LangGraph (optional AI matching path)
- RapidFuzz (fuzzy name matching)
- pdfplumber (PDF text extraction)
- ag-ui-protocol (live progress streaming)
- Gemini (assistant intent classification when keyed)
- Groq (primary AG-UI workspace planner when keyed)
- SQLite (persistence)
- React + Vite (frontend)

---

## Running it

```bash
git clone https://github.com/VedantSinghThakur21/reconciliation-agentic.git
cd reconciliation-agentic

python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -r requirements.txt

cd ui && npm install && cd ..

cp .env.example .env
# Optional: GEMINI_API_KEY (assistant classify), GROQ_API_KEY (AG-UI planner),
# CREWAI_API_URL + CREWAI_BEARER_TOKEN (leftover enrichment).
# Core StrictMatcher pipeline runs fully without these — enrichment fails open.

# Classic CLI pipeline (CSV invoices × payments)
python scripts/run_pipeline.py \
  --invoices data/demo/quickbooks_invoices.csv \
  --payments data/demo/bank_transactions.csv

# Optional leftover enrichment
python scripts/run_pipeline.py \
  --invoices data/demo/quickbooks_invoices.csv \
  --payments data/demo/bank_transactions.csv \
  --use-crewai

# Finance Controller eval batch (the 92.0% run above)
python scripts/generate_fc_demo_data.py
python scripts/run_finance_flow.py

# API + UI
python scripts/run_api.py          # http://127.0.0.1:8003
cd ui && npm run dev               # http://localhost:5173

# Tests
python -m pytest
```

---

## What's next

- OCR support for scanned PDF bank statements (text-layer extraction already exists; OCR is the fallback when extraction returns empty)
- Expanding AG-UI streaming coverage beyond the AMP enrichment path
- GST/TDS-aware deduction matching
- Broader finance-ops loops: PO matching, credit-note netting, multi-entity reconciliation

---

*Built by Vedant Singh Thakur as part of ReconIQ, an in-development reconciliation platform. This repo contains the sanitized, standalone core engine — extracted for this submission from the full product.*
