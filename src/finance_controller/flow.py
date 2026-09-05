from __future__ import annotations

"""
AI Finance Controller — CrewAI Flow

Architecture matches the product brief:
  conversational_intake → extract_bank_pdf → ingest_and_validate → normalize →
  generate_candidates → score_candidates → route_decisions → investigate_exceptions →
  human_review → persist_results → create_journal_entries → evaluate → summary_report
"""

import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from crewai.flow.flow import Flow, listen, router, start

from src.finance_controller.db import FinanceDB
from src.finance_controller.review_state import (
    DECISION_TO_RESULT_STATUS,
    HITL_ACTIONS,
    pending_review_items,
)
from src.finance_controller.utils import (
    amount_score,
    confidence_score,
    date_score,
    merchant_score,
    new_id,
    normalize_currency,
    normalize_merchant,
    normalize_reference,
    parse_date,
    read_csv,
    reference_score,
    stub_erp_transactions,
)

os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "demo"


class FinanceState(BaseModel):
    user_request: str = "Run AR reconciliation on demo feeds"
    bank_csv_path: str = str(DEMO / "bank.csv")
    payment_processor_csv_path: str = str(DEMO / "payment_processor.csv")
    ground_truth_csv_path: str = str(DEMO / "ground_truth.csv")
    bank_pdf_path: str | None = None
    ar_ap_mode: Literal["AP", "AR", "BOTH"] = "AR"

    run_id: str = ""
    confirmation_message: str = ""
    stages: list[dict[str, Any]] = Field(default_factory=list)

    raw_records_summary: dict[str, Any] = Field(default_factory=dict)
    normalized_records_summary: dict[str, Any] = Field(default_factory=dict)
    candidates_summary: dict[str, Any] = Field(default_factory=dict)
    scored_candidates_summary: dict[str, Any] = Field(default_factory=dict)
    investigation_summary: dict[str, Any] = Field(default_factory=dict)
    human_review_summary: dict[str, Any] = Field(default_factory=dict)
    persistence_summary: dict[str, Any] = Field(default_factory=dict)
    journal_entries_summary: dict[str, Any] = Field(default_factory=dict)
    evaluation_metrics: dict[str, Any] = Field(default_factory=dict)

    auto_match_count: int = 0
    review_band_count: int = 0
    exception_count: int = 0
    final_report: str = ""

    # in-memory working sets (not persisted as columns)
    _db_path: str = str(ROOT / "data" / "finance_controller.db")


class AIFinanceController(Flow[FinanceState]):
    """CrewAI Flow — autonomous multi-source financial reconciliation."""

    def __init__(self, db_path: str | Path | None = None, **data: Any):
        super().__init__(**data)
        if db_path:
            self.state._db_path = str(db_path)
        self._db = FinanceDB(self.state._db_path)

    # ── helpers ───────────────────────────────────────────────────────

    def _stage(self, name: str, status: str, detail: dict[str, Any] | None = None) -> None:
        stages = list(self.state.stages)
        entry = {"name": name, "status": status, "at": time.time(), "detail": detail or {}}
        for s in stages:
            if s["name"] == name:
                s.update(entry)
                break
        else:
            stages.append(entry)
        self.state.stages = stages
        if self.state.run_id:
            self._db.update_stages(self.state.run_id, stages)
            self._db.audit(f"stage_{status}", f"Stage {name}: {status}", entity_id=name, payload=detail, run_id=self.state.run_id)

    # ── 1. conversational_intake ──────────────────────────────────────

    @start()
    def conversational_intake(self) -> str:
        self._stage("conversational_intake", "running")
        req = (self.state.user_request or "").lower()

        if "ap" in req and "ar" not in req:
            self.state.ar_ap_mode = "AP"
        elif "both" in req:
            self.state.ar_ap_mode = "BOTH"
        elif "ar" in req:
            self.state.ar_ap_mode = "AR"

        # Extract quoted/path-like tokens if present
        paths = re.findall(r"[\w./\\-]+\.csv", self.state.user_request or "")
        if len(paths) >= 1 and Path(paths[0]).exists():
            self.state.bank_csv_path = paths[0]
        if len(paths) >= 2 and Path(paths[1]).exists():
            self.state.payment_processor_csv_path = paths[1]
        if len(paths) >= 3 and Path(paths[2]).exists():
            self.state.ground_truth_csv_path = paths[2]

        # defaults
        if not self.state.bank_csv_path:
            self.state.bank_csv_path = str(DEMO / "bank.csv")
        if not self.state.payment_processor_csv_path:
            self.state.payment_processor_csv_path = str(DEMO / "payment_processor.csv")
        if not self.state.ground_truth_csv_path:
            self.state.ground_truth_csv_path = str(DEMO / "ground_truth.csv")

        self.state.run_id = self._db.create_run(
            self.state.user_request,
            self.state.ar_ap_mode,
            bank_pdf_path=self.state.bank_pdf_path,
        )
        self.state.confirmation_message = (
            f"Understood. Running {self.state.ar_ap_mode} reconciliation on "
            f"bank=`{Path(self.state.bank_csv_path).name}`, "
            f"processor=`{Path(self.state.payment_processor_csv_path).name}` "
            f"(run {self.state.run_id})."
        )
        self._stage("conversational_intake", "completed", {
            "ar_ap_mode": self.state.ar_ap_mode,
            "confirmation_message": self.state.confirmation_message,
        })
        return "intake_ok"

    # ── 2. extract_bank_pdf ───────────────────────────────────────────

    @listen(conversational_intake)
    def extract_bank_pdf(self) -> str:
        self._stage("extract_bank_pdf", "running")
        if not self.state.bank_pdf_path:
            detail = {"skipped": True, "records_extracted": 0, "csv_written_to": None}
            self._stage("extract_bank_pdf", "completed", detail)
            return "pdf_skipped"

        pdf_path = Path(self.state.bank_pdf_path)
        if not pdf_path.is_file():
            # Allow bare filenames relative to uploads/ or demo/
            for base in (ROOT / "data" / "uploads", ROOT / "data" / "demo", ROOT / "data" / "synthetic" / "bank_statements"):
                cand = base / Path(self.state.bank_pdf_path).name
                if cand.is_file():
                    pdf_path = cand
                    break

        if not pdf_path.is_file():
            detail = {
                "skipped": True,
                "records_extracted": 0,
                "error": f"PDF not found: {self.state.bank_pdf_path}",
                "note": "CrewAI AMP OCR can still process bank_pdf_path when the file exists in the deployment workspace.",
            }
            self._stage("extract_bank_pdf", "completed", detail)
            return "pdf_skipped"

        try:
            from src.ingestion.pdf_loader import PDFParseError, parse_payment_pdf

            payments = parse_payment_pdf(pdf_path)
        except Exception as e:  # noqa: BLE001 — text-layer may fail on scans; AMP OCR handles those
            detail = {
                "skipped": True,
                "records_extracted": 0,
                "pdf": str(pdf_path),
                "error": str(e),
                "note": (
                    "Local text-layer extract failed (common for scanned PDFs). "
                    "CrewAI AMP includes OCR — prefer engine=amp for image statements."
                ),
            }
            self._stage("extract_bank_pdf", "completed", detail)
            return "pdf_skipped"

        out_dir = ROOT / "data" / "uploads"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"bank_from_{pdf_path.stem}.csv"
        import csv as _csv

        with out_csv.open("w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(
                f,
                fieldnames=["transaction_id", "date", "amount", "currency", "merchant", "reference", "description"],
            )
            w.writeheader()
            for p in payments:
                w.writerow({
                    "transaction_id": p.get("id"),
                    "date": p.get("date"),
                    "amount": p.get("amount"),
                    "currency": p.get("currency") or "INR",
                    "merchant": p.get("senderName"),
                    "reference": p.get("reference"),
                    "description": p.get("reference") or p.get("senderName"),
                })

        self.state.bank_csv_path = str(out_csv)
        detail = {
            "skipped": False,
            "records_extracted": len(payments),
            "csv_written_to": str(out_csv),
            "pdf": str(pdf_path),
            "extractor": "pdfplumber_text_layer",
        }
        self._stage("extract_bank_pdf", "completed", detail)
        return "pdf_ok"

    # ── 3. ingest_and_validate ────────────────────────────────────────

    @listen(extract_bank_pdf)
    def ingest_and_validate(self) -> str:
        self._stage("ingest_and_validate", "running")
        from src.finance_controller.utils import DEMO_ERP_COUNT

        erp = stub_erp_transactions(DEMO_ERP_COUNT, mode=self.state.ar_ap_mode)
        bank = read_csv(Path(self.state.bank_csv_path))
        pp = read_csv(Path(self.state.payment_processor_csv_path))

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        dup = 0
        missing = 0

        def add(source: str, row: dict[str, Any]) -> None:
            nonlocal dup, missing
            tid = str(row.get("transaction_id") or "").strip()
            if not tid:
                missing += 1
                return
            if tid in seen:
                dup += 1
            seen.add(tid)
            if row.get("amount") in (None, ""):
                missing += 1
            records.append({
                "id": new_id("SRC"),
                "source": source,
                "transaction_id": tid,
                "txn_date": row.get("date"),
                "amount": float(row.get("amount") or 0),
                "currency": row.get("currency") or "INR",
                "merchant": row.get("merchant") or "",
                "reference": row.get("reference") or "",
                "description": row.get("description") or "",
                "raw": row,
            })

        for r in erp:
            add("erp", r)
        for r in bank:
            add("bank", r)
        for r in pp:
            add("payment_processor", r)

        self._db.insert_sources(records, run_id=self.state.run_id)
        summary = {
            "total_records_per_source": {
                "erp": len(erp),
                "bank": len(bank),
                "payment_processor": len(pp),
            },
            "duplicate_count": dup,
            "missing_field_count": missing,
            "validation_status": "passed" if missing == 0 else "warnings",
        }
        self.state.raw_records_summary = summary
        self._stage("ingest_and_validate", "completed", summary)
        return "ingest_ok"

    # ── 4. normalize ──────────────────────────────────────────────────

    @listen(ingest_and_validate)
    def normalize(self) -> str:
        self._stage("normalize", "running")
        rows = self._db.fetch_sources(self.state.run_id)
        alias_hits = 0
        date_fixes = 0
        ccy_fixes = 0
        for r in rows:
            merchant_norm = normalize_merchant(r.get("merchant") or "")
            if merchant_norm != (r.get("merchant") or ""):
                alias_hits += 1
            d = parse_date(r.get("txn_date"))
            date_norm = d.isoformat() if d else (r.get("txn_date") or "")
            if date_norm and date_norm != (r.get("txn_date") or ""):
                date_fixes += 1
            ccy = normalize_currency(r.get("currency") or "INR")
            if ccy != (r.get("currency") or ""):
                ccy_fixes += 1
            fields = {
                "merchant_norm": merchant_norm,
                "date_norm": date_norm,
                "amount_norm": round(float(r.get("amount") or 0), 2),
                "currency_norm": ccy,
                "reference_norm": normalize_reference(r.get("reference") or ""),
            }
            self._db.update_normalization(r["id"], fields)
        self._db.commit()
        summary = {
            "records_normalized": len(rows),
            "merchant_aliases_applied": alias_hits,
            "date_corrections": date_fixes,
            "currency_normalizations": ccy_fixes,
        }
        self.state.normalized_records_summary = summary
        self._stage("normalize", "completed", summary)
        return "normalize_ok"

    # ── 5. generate_candidates ────────────────────────────────────────

    @listen(normalize)
    def generate_candidates(self) -> str:
        self._stage("generate_candidates", "running")
        erp = self._db.fetch_sources(self.state.run_id, "erp")
        bank = self._db.fetch_sources(self.state.run_id, "bank")
        pp = self._db.fetch_sources(self.state.run_id, "payment_processor")
        counters = bank + pp

        candidates: list[dict[str, Any]] = []
        by_strategy = {"EXACT": 0, "FUZZY": 0, "AGGREGATION": 0, "PARTIAL_SETTLEMENT": 0}
        erp_without: list[str] = []

        used_counter: set[str] = set()

        for e in erp:
            e_amt = float(e.get("amount_norm") or 0)
            e_date = parse_date(e.get("date_norm"))
            e_ref = e.get("reference_norm") or ""
            e_merch = e.get("merchant_norm") or ""
            found = False

            # EXACT
            for c in counters:
                if c["transaction_id"] in used_counter:
                    continue
                c_amt = float(c.get("amount_norm") or 0)
                c_date = parse_date(c.get("date_norm"))
                c_ref = c.get("reference_norm") or ""
                if abs(e_amt - c_amt) < 0.01 and e_ref and e_ref == c_ref and e_date and c_date and e_date == c_date:
                    candidates.append({
                        "id": new_id("CAND"),
                        "erp_txn_id": e["transaction_id"],
                        "counter_txn_id": c["transaction_id"],
                        "strategy": "EXACT",
                        "evidence": {"amount": e_amt, "reference": e_ref},
                    })
                    by_strategy["EXACT"] += 1
                    used_counter.add(c["transaction_id"])
                    found = True
                    break
            if found:
                continue

            # FUZZY
            best = None
            best_ratio = 0.0
            for c in counters:
                if c["transaction_id"] in used_counter:
                    continue
                c_amt = float(c.get("amount_norm") or 0)
                c_date = parse_date(c.get("date_norm"))
                c_merch = c.get("merchant_norm") or ""
                ratio = merchant_score(e_merch, c_merch) * 100
                amt_ok = abs(e_amt - c_amt) / max(e_amt, 0.01) <= 0.01
                date_ok = e_date and c_date and abs((e_date - c_date).days) <= 3
                if ratio >= 70 and amt_ok and date_ok and ratio > best_ratio:
                    best = c
                    best_ratio = ratio
            if best:
                candidates.append({
                    "id": new_id("CAND"),
                    "erp_txn_id": e["transaction_id"],
                    "counter_txn_id": best["transaction_id"],
                    "strategy": "FUZZY",
                    "evidence": {"token_sort_ratio": best_ratio},
                })
                by_strategy["FUZZY"] += 1
                used_counter.add(best["transaction_id"])
                continue

            # PARTIAL SETTLEMENT 50% / 25%
            partial_hit = None
            for c in counters:
                if c["transaction_id"] in used_counter:
                    continue
                c_amt = float(c.get("amount_norm") or 0)
                c_date = parse_date(c.get("date_norm"))
                if not e_date or not c_date or abs((e_date - c_date).days) > 7:
                    continue
                for frac in (0.5, 0.25):
                    if abs(c_amt - e_amt * frac) < 0.05:
                        partial_hit = c
                        break
                if partial_hit:
                    break
            if partial_hit:
                candidates.append({
                    "id": new_id("CAND"),
                    "erp_txn_id": e["transaction_id"],
                    "counter_txn_id": partial_hit["transaction_id"],
                    "strategy": "PARTIAL_SETTLEMENT",
                    "evidence": {"erp_amount": e_amt, "paid": float(partial_hit.get("amount_norm") or 0)},
                })
                by_strategy["PARTIAL_SETTLEMENT"] += 1
                used_counter.add(partial_hit["transaction_id"])
                continue

            # AGGREGATION: 2-3 counters sum to ERP within 5 days
            window = [
                c for c in counters
                if c["transaction_id"] not in used_counter
                and e_date and parse_date(c.get("date_norm"))
                and abs((e_date - parse_date(c.get("date_norm"))).days) <= 5  # type: ignore[operator]
            ]
            agg_found = False
            for i in range(len(window)):
                for j in range(i + 1, len(window)):
                    s2 = float(window[i].get("amount_norm") or 0) + float(window[j].get("amount_norm") or 0)
                    if abs(s2 - e_amt) <= 0.01:
                        for c in (window[i], window[j]):
                            candidates.append({
                                "id": new_id("CAND"),
                                "erp_txn_id": e["transaction_id"],
                                "counter_txn_id": c["transaction_id"],
                                "strategy": "AGGREGATION",
                                "evidence": {"group_sum": s2},
                            })
                            used_counter.add(c["transaction_id"])
                        by_strategy["AGGREGATION"] += 1
                        agg_found = True
                        break
                if agg_found:
                    break
            if not agg_found:
                erp_without.append(e["transaction_id"])

        self._db.insert_candidates(candidates, run_id=self.state.run_id)
        summary = {
            "total_candidates": len(candidates),
            "candidates_by_strategy": by_strategy,
            "erp_records_with_no_candidates": erp_without,
        }
        self.state.candidates_summary = summary
        self._stage("generate_candidates", "completed", summary)
        return "candidates_ok"

    # ── 6. score_candidates ───────────────────────────────────────────

    @listen(generate_candidates)
    def score_candidates(self) -> str:
        self._stage("score_candidates", "running")
        by_id = {r["transaction_id"]: r for r in self._db.fetch_sources(self.state.run_id)}
        cands = self._db.fetch_candidates(self.state.run_id)
        scored = []
        scores = []
        for c in cands:
            erp = by_id.get(c["erp_txn_id"], {})
            ctr = by_id.get(c["counter_txn_id"], {})
            a_s = amount_score(float(erp.get("amount_norm") or 0), float(ctr.get("amount_norm") or 0))
            m_s = merchant_score(erp.get("merchant_norm") or "", ctr.get("merchant_norm") or "")
            d_s = date_score(parse_date(erp.get("date_norm")), parse_date(ctr.get("date_norm")))
            r_s = reference_score(erp.get("reference_norm") or "", ctr.get("reference_norm") or "")
            conf = confidence_score(a_s, m_s, d_s, r_s)
            # strategy boosts
            if c["strategy"] == "EXACT":
                conf = max(conf, 0.96)
            elif c["strategy"] == "PARTIAL_SETTLEMENT":
                conf = min(conf, 0.78)
            scored.append({
                **c,
                "amount_score": a_s,
                "merchant_score": m_s,
                "date_score": d_s,
                "reference_score": r_s,
                "confidence": conf,
                "evidence": {"strategy": c["strategy"]},
            })
            scores.append(conf)

        self._db.clear_candidates(self.state.run_id)
        self._db.commit()
        self._db.insert_candidates(scored, run_id=self.state.run_id)

        bands = {
            "auto_match_ge_0_90": sum(1 for s in scores if s >= 0.90),
            "review_0_70_0_89": sum(1 for s in scores if 0.70 <= s < 0.90),
            "exception_lt_0_70": sum(1 for s in scores if s < 0.70),
        }
        summary = {
            "total_scored": len(scored),
            "score_distribution": bands,
            "highest_score": max(scores) if scores else 0,
            "lowest_score": min(scores) if scores else 0,
            "average_score": round(sum(scores) / len(scores), 4) if scores else 0,
        }
        self.state.scored_candidates_summary = summary
        self.state.auto_match_count = bands["auto_match_ge_0_90"]
        self.state.review_band_count = bands["review_0_70_0_89"]
        self.state.exception_count = bands["exception_lt_0_70"]
        self._stage("score_candidates", "completed", summary)
        return "scored_ok"

    # ── 7. route_decisions ────────────────────────────────────────────

    @router(score_candidates)
    def route_decisions(self) -> str:
        self._stage("route_decisions", "completed", {
            "emits": "proceed_to_investigation",
            "bands": {
                "AUTO_MATCH": ">=0.90",
                "HUMAN_REVIEW": "0.70-0.89",
                "EXCEPTION": "<0.70",
            },
        })
        return "proceed_to_investigation"

    # ── 8. investigate_exceptions ─────────────────────────────────────

    @listen("proceed_to_investigation")
    def investigate_exceptions(self) -> str:
        self._stage("investigate_exceptions", "running")
        sources = self._db.fetch_sources(self.state.run_id)
        by_id = {r["transaction_id"]: r for r in sources}
        cands = self._db.fetch_candidates(self.state.run_id)
        best_by_erp: dict[str, dict] = {}
        best_by_counter: dict[str, dict] = {}
        counts_by_erp: dict[str, int] = {}
        for c in cands:
            counts_by_erp[c["erp_txn_id"]] = counts_by_erp.get(c["erp_txn_id"], 0) + 1
            prev = best_by_erp.get(c["erp_txn_id"])
            if not prev or float(c.get("confidence") or 0) > float(prev.get("confidence") or 0):
                best_by_erp[c["erp_txn_id"]] = c
            prev_c = best_by_counter.get(c["counter_txn_id"])
            if not prev_c or float(c.get("confidence") or 0) > float(prev_c.get("confidence") or 0):
                best_by_counter[c["counter_txn_id"]] = c

        exceptions: list[dict[str, Any]] = []
        by_type: dict[str, int] = {}

        def add_exc(txn_id: str, etype: str, action: str, conf: float, details: dict) -> None:
            exceptions.append({
                "id": new_id("EXC"),
                "transaction_id": txn_id,
                "exception_type": etype,
                "recommended_action": action,
                "confidence": conf,
                "details": details,
            })
            by_type[etype] = by_type.get(etype, 0) + 1

        matched_erp = set()
        matched_counter = set()
        for erp_id, c in best_by_erp.items():
            conf = float(c.get("confidence") or 0)
            if conf >= 0.90 and c.get("strategy") != "PARTIAL_SETTLEMENT":
                matched_erp.add(erp_id)
                matched_counter.add(c["counter_txn_id"])
                continue
            if c.get("strategy") == "PARTIAL_SETTLEMENT":
                add_exc(c["counter_txn_id"], "PARTIAL_SETTLEMENT", "RESOLVE", conf, {"erp": erp_id})
                matched_erp.add(erp_id)
                matched_counter.add(c["counter_txn_id"])
                continue
            if conf >= 0.70:
                add_exc(c["counter_txn_id"], "MISMATCH" if conf < 0.8 else "MULTIPLE_CANDIDATES",
                        "REVIEW", conf, {"erp": erp_id, "strategy": c.get("strategy")})
                continue
            add_exc(c["counter_txn_id"], "MISMATCH", "INVESTIGATE", conf, {"erp": erp_id})

        for e in [r for r in sources if r["source"] == "erp"]:
            if e["transaction_id"] not in matched_erp and e["transaction_id"] not in best_by_erp:
                add_exc(e["transaction_id"], "MISSING_BANK_RECORD", "APPROVE_IF_IMMATERIAL", 0.0, {})

        for c in [r for r in sources if r["source"] in ("bank", "payment_processor")]:
            if c["transaction_id"] not in matched_counter and c["transaction_id"] not in best_by_counter:
                add_exc(c["transaction_id"], "MISSING_ERP_RECORD", "ESCALATE", 0.0, {})

        # duplicates: same counter appearing in >1 candidate at high score
        for erp_id, n in counts_by_erp.items():
            if n > 1:
                add_exc(erp_id, "MULTIPLE_CANDIDATES", "REVIEW", float(best_by_erp.get(erp_id, {}).get("confidence") or 0), {"count": n})

        self._db.insert_exceptions(exceptions, run_id=self.state.run_id)
        by_action: dict[str, int] = {}
        for e in exceptions:
            a = e.get("recommended_action") or "NONE"
            by_action[a] = by_action.get(a, 0) + 1
        summary = {
            "total_exceptions": len(exceptions),
            "exceptions_by_type": by_type,
            "exceptions_by_recommended_action": by_action,
        }
        self.state.investigation_summary = summary
        self._stage("investigate_exceptions", "completed", summary)
        return "investigate_ok"

    # ── 9. human_review ───────────────────────────────────────────────

    @listen(investigate_exceptions)
    def human_review(self) -> str:
        self._stage("human_review", "running")
        cands = {c["counter_txn_id"]: c for c in self._db.fetch_candidates(self.state.run_id)}
        # also index by erp
        cands_erp = {c["erp_txn_id"]: c for c in self._db.fetch_candidates(self.state.run_id)}
        reviews = []
        counts = {"APPROVED": 0, "REJECTED": 0, "RESOLVED": 0, "ESCALATED": 0}
        pending_records: list[dict[str, Any]] = []

        for ex in self._db.fetch_exceptions(self.state.run_id):
            et = ex["exception_type"]
            action = (ex.get("recommended_action") or "").upper()
            conf = float(ex.get("confidence") or 0)
            cand = cands.get(ex["transaction_id"]) or cands_erp.get(ex["transaction_id"])
            amount_s = float((cand or {}).get("amount_score") or 0)

            # True HITL: leave REVIEW / ESCALATE / INVESTIGATE for the UI Reviews queue
            if action in HITL_ACTIONS:
                pending_records.append(
                    {
                        "exception_id": ex["id"],
                        "transaction_id": ex["transaction_id"],
                        "exception_type": et,
                        "recommended_action": action,
                        "confidence": conf,
                    }
                )
                self._db.audit(
                    "human_review_pending",
                    f"{ex['transaction_id']} awaiting analyst ({et}/{action})",
                    entity_id=ex["id"],
                    run_id=self.state.run_id,
                )
                continue

            if et == "PARTIAL_SETTLEMENT":
                decision = "RESOLVED"
            elif et == "MISSING_BANK_RECORD":
                erp = next((r for r in self._db.fetch_sources(self.state.run_id, "erp") if r["transaction_id"] == ex["transaction_id"]), None)
                amt = float((erp or {}).get("amount_norm") or 0)
                decision = "APPROVED" if amt < 100 else "ESCALATED"
            elif et == "MISSING_ERP_RECORD":
                decision = "ESCALATED"
            elif et == "MISMATCH":
                decision = "REJECTED"
            elif 0.70 <= conf < 0.90:
                if conf >= 0.80 and amount_s >= 0.95:
                    decision = "APPROVED"
                elif amount_s < 0.98 and amount_s > 0 and (1 - amount_s) > 0.02:
                    decision = "REJECTED"
                else:
                    decision = "RESOLVED"
            else:
                decision = "RESOLVED"

            reviews.append({
                "id": new_id("REV"),
                "transaction_id": ex["transaction_id"],
                "decision": decision,
                "reason": f"Auto human-review policy for {et}",
                "confidence": conf,
            })
            counts[decision] = counts.get(decision, 0) + 1
            self._db.audit("human_review", f"{ex['transaction_id']} → {decision}", entity_id=ex["id"], run_id=self.state.run_id)

        self._db.insert_reviews(reviews, run_id=self.state.run_id)
        item_ids = [p["transaction_id"] for p in pending_records]
        pending_hitl = len(pending_records)
        summary = {
            "total_reviewed": len(reviews),
            "decisions_applied": len(reviews),
            "pending_hitl": pending_hitl,
            "pending_human_count": pending_hitl,
            "item_ids": item_ids,
            "pending_items": pending_records,
            **{f"{k.lower()}_count": v for k, v in counts.items()},
        }
        self.state.human_review_summary = summary
        self._stage("human_review", "completed", summary)
        return "review_ok"

    # ── 10. persist_results ───────────────────────────────────────────

    @listen(human_review)
    def persist_results(self) -> str:
        self._stage("persist_results", "running")
        cands = self._db.fetch_candidates(self.state.run_id)
        best_by_erp = {}
        for c in cands:
            prev = best_by_erp.get(c["erp_txn_id"])
            if not prev or float(c.get("confidence") or 0) > float(prev.get("confidence") or 0):
                best_by_erp[c["erp_txn_id"]] = c

        reviews = {r["transaction_id"]: r for r in self._db.fetch_reviews(self.state.run_id)}
        sources = self._db.fetch_sources(self.state.run_id)
        by_id = {r["transaction_id"]: r for r in sources}
        pending_txns = {
            p["transaction_id"]
            for p in (self.state.human_review_summary.get("pending_items") or [])
        }
        if not pending_txns:
            pending_txns = {
                e["transaction_id"]
                for e in self._db.fetch_exceptions(self.state.run_id)
                if e["transaction_id"] not in reviews
                and str(e.get("recommended_action") or "").upper() in HITL_ACTIONS
            }

        results = []
        by_status: dict[str, int] = {}
        reconciled_amt = 0.0
        unmatched_amt = 0.0
        handled_counters: set[str] = set()

        for erp_id, c in best_by_erp.items():
            conf = float(c.get("confidence") or 0)
            counter = c["counter_txn_id"]
            handled_counters.add(counter)
            review = reviews.get(counter) or reviews.get(erp_id)
            if conf >= 0.90 and c.get("strategy") != "PARTIAL_SETTLEMENT" and not review:
                status = "AUTO_MATCHED"
            elif review:
                d = review["decision"]
                status = DECISION_TO_RESULT_STATUS.get(d, "EXCEPTION_RESOLVED")
                if c.get("strategy") == "PARTIAL_SETTLEMENT" and d == "RESOLVED":
                    status = "EXCEPTION_RESOLVED"
            elif counter in pending_txns or erp_id in pending_txns:
                status = "HUMAN_REVIEW"
            else:
                status = "UNMATCHED"

            amt = float(by_id.get(counter, {}).get("amount_norm") or by_id.get(erp_id, {}).get("amount_norm") or 0)
            if status in ("AUTO_MATCHED", "HUMAN_APPROVED", "HUMAN_RESOLVED", "EXCEPTION_RESOLVED"):
                reconciled_amt += amt
            else:
                unmatched_amt += amt

            results.append({
                "id": new_id("RES"),
                "transaction_id": counter,
                "matched_txn_id": erp_id,
                "final_status": status,
                "confidence": conf,
                "reconciled_amount": amt if status.startswith("AUTO") or "APPROVED" in status or "RESOLVED" in status else 0,
                "strategy": c.get("strategy"),
                "details": {"erp": erp_id},
            })
            by_status[status] = by_status.get(status, 0) + 1

        for r in sources:
            if r["source"] not in ("bank", "payment_processor"):
                continue
            if r["transaction_id"] in handled_counters:
                continue
            review = reviews.get(r["transaction_id"])
            status = "UNMATCHED"
            if review:
                status = DECISION_TO_RESULT_STATUS.get(review["decision"], "UNMATCHED")
            elif r["transaction_id"] in pending_txns:
                status = "HUMAN_REVIEW"
            amt = float(r.get("amount_norm") or 0)
            unmatched_amt += amt
            results.append({
                "id": new_id("RES"),
                "transaction_id": r["transaction_id"],
                "matched_txn_id": None,
                "final_status": status,
                "confidence": 0,
                "reconciled_amount": 0,
                "strategy": None,
                "details": {},
            })
            by_status[status] = by_status.get(status, 0) + 1

        existing_txns = {row["transaction_id"] for row in results}
        for tid in pending_txns:
            if tid in existing_txns:
                continue
            src = by_id.get(tid) or {}
            amt = float(src.get("amount_norm") or src.get("amount") or 0)
            unmatched_amt += amt
            results.append(
                {
                    "id": new_id("RES"),
                    "transaction_id": tid,
                    "matched_txn_id": None,
                    "final_status": "HUMAN_REVIEW",
                    "confidence": 0,
                    "reconciled_amount": 0,
                    "strategy": None,
                    "details": {"pending_human": True},
                }
            )
            by_status["HUMAN_REVIEW"] = by_status.get("HUMAN_REVIEW", 0) + 1
            existing_txns.add(tid)

        self._db.insert_results(results, run_id=self.state.run_id)
        summary = {
            "total_results": len(results),
            "results_by_status": by_status,
            "total_reconciled_amount": round(reconciled_amt, 2),
            "total_unmatched_amount": round(unmatched_amt, 2),
        }
        self.state.persistence_summary = summary
        self._stage("persist_results", "completed", summary)
        return "persist_ok"

    # ── 11. create_journal_entries ────────────────────────────────────

    @listen(persist_results)
    def create_journal_entries(self) -> str:
        self._stage("create_journal_entries", "running")
        mode = self.state.ar_ap_mode
        results = self._db.fetch_results(self.state.run_id)
        journals = []
        writebacks = []

        def add_je(entry_type: str, status: str, debit: str, credit: str, amount: float, txn: str, memo: str) -> None:
            jid = new_id("JE")
            journals.append({
                "id": jid,
                "entry_type": entry_type,
                "status": status,
                "debit_account": debit,
                "credit_account": credit,
                "amount": amount,
                "currency": "INR",
                "transaction_id": txn,
                "memo": memo,
            })
            wb_status = "SUCCESS" if status == "POSTED" else "PENDING"
            writebacks.append({
                "id": new_id("WB"),
                "journal_id": jid,
                "status": wb_status,
                "message": f"ERP writeback {wb_status} for {txn}",
            })

        for r in results:
            amt = float(r.get("reconciled_amount") or 0) or float(
                next((s.get("amount_norm") or 0 for s in self._db.fetch_sources(self.state.run_id) if s["transaction_id"] == r["transaction_id"]), 0)
            )
            status = r["final_status"]
            txn = r["transaction_id"]

            modes = ["AR", "AP"] if mode == "BOTH" else [mode]
            for m in modes:
                if m == "AR":
                    if status in ("AUTO_MATCHED", "HUMAN_APPROVED"):
                        add_je("AR", "POSTED", "1010", "1200", amt, txn, "Cash receipt vs AR")
                    elif status in ("HUMAN_RESOLVED", "EXCEPTION_RESOLVED"):
                        half = round(amt / 2, 2)
                        add_je("AR", "POSTED", "1010", "1200", half, txn, "Resolved split — cash/AR")
                        add_je("ADJ", "POSTED", "1010", "9999", amt - half, txn, "Resolved split — suspense")
                    elif status in ("HUMAN_REJECTED", "UNMATCHED", "HUMAN_REVIEW", "PENDING"):
                        add_je("AR", "DRAFT", "9999", "1200", amt, txn, "Unmatched / rejected — suspense")
                else:  # AP
                    if status in ("AUTO_MATCHED", "HUMAN_APPROVED"):
                        add_je("AP", "POSTED", "2000", "1010", amt, txn, "AP settlement vs cash")
                    elif status in ("HUMAN_RESOLVED", "EXCEPTION_RESOLVED"):
                        half = round(amt / 2, 2)
                        add_je("AP", "POSTED", "2000", "1010", half, txn, "Resolved split — AP/cash")
                        add_je("ADJ", "POSTED", "2000", "9999", amt - half, txn, "Resolved split — suspense")
                    elif status in ("HUMAN_REJECTED", "UNMATCHED", "HUMAN_REVIEW", "PENDING"):
                        add_je("AP", "DRAFT", "9999", "2000", amt, txn, "Unmatched / rejected — suspense")

        self._db.insert_journals(journals, run_id=self.state.run_id)
        self._db.insert_writebacks(writebacks, run_id=self.state.run_id)
        by_type: dict[str, int] = {}
        by_st: dict[str, int] = {}
        for j in journals:
            by_type[j["entry_type"]] = by_type.get(j["entry_type"], 0) + 1
            by_st[j["status"]] = by_st.get(j["status"], 0) + 1
        summary = {
            "total_entries_created": len(journals),
            "entries_by_type": by_type,
            "entries_by_status": by_st,
            "writeback_success_count": sum(1 for w in writebacks if w["status"] == "SUCCESS"),
            "writeback_pending_count": sum(1 for w in writebacks if w["status"] == "PENDING"),
        }
        self.state.journal_entries_summary = summary
        self._stage("create_journal_entries", "completed", summary)
        return "journals_ok"

    # ── 12. evaluate ──────────────────────────────────────────────────

    @listen(create_journal_entries)
    def evaluate(self) -> str:
        self._stage("evaluate", "running")
        gt_path = Path(self.state.ground_truth_csv_path)
        gt_rows = read_csv(gt_path) if gt_path.exists() else []
        results = {r["transaction_id"]: r for r in self._db.fetch_results(self.state.run_id)}
        # also allow lookup by matched erp id
        by_erp = {r.get("matched_txn_id"): r for r in self._db.fetch_results(self.state.run_id) if r.get("matched_txn_id")}

        positives = {"AUTO_MATCHED", "HUMAN_APPROVED", "HUMAN_RESOLVED", "EXCEPTION_RESOLVED"}
        negatives = {"HUMAN_REJECTED", "UNMATCHED", "HUMAN_REVIEW", "PENDING"}

        tp = fp = fn = tn = 0
        details = []
        for g in gt_rows:
            tid = g.get("transaction_id") or ""
            expected_status = (g.get("expected_status") or "").upper()
            expected_match = (g.get("expected_match_id") or "").strip() or None
            pred = results.get(tid) or by_erp.get(tid)
            pred_status = (pred or {}).get("final_status") or "UNMATCHED"
            pred_match = (pred or {}).get("matched_txn_id")
            expected_pos = expected_status == "MATCH"
            predicted_pos = pred_status in positives

            correct = False
            if expected_pos and predicted_pos:
                if expected_match is None or pred_match == expected_match or tid == expected_match:
                    tp += 1
                    correct = True
                else:
                    fp += 1
                    fn += 1
            elif expected_pos and not predicted_pos:
                fn += 1
            elif not expected_pos and predicted_pos:
                fp += 1
            else:
                tn += 1
                correct = True

            details.append({
                "transaction_id": tid,
                "expected_status": expected_status,
                "predicted_status": pred_status,
                "correct": correct,
            })

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        total = len(gt_rows) or 1
        accuracy = sum(1 for d in details if d["correct"]) / total
        match_rate = sum(1 for r in self._db.fetch_results(self.state.run_id) if r["final_status"] in positives) / max(len(self._db.fetch_results(self.state.run_id)), 1)
        false_match_rate = fp / max(tp + fp, 1)

        metrics = {
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "match_rate": round(match_rate, 4),
            "false_match_rate": round(false_match_rate, 4),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "details": details,
        }
        self._db.save_metrics(metrics, run_id=self.state.run_id)
        self.state.evaluation_metrics = metrics
        self._stage("evaluate", "completed", {k: metrics[k] for k in ("accuracy", "precision", "recall", "f1", "match_rate")})
        return "evaluate_ok"

    # ── 13. summary_report ────────────────────────────────────────────

    @listen(evaluate)
    def summary_report(self) -> str:
        self._stage("summary_report", "running")
        m = self.state.evaluation_metrics
        p = self.state.persistence_summary
        j = self.state.journal_entries_summary
        h = self.state.human_review_summary or {}
        # Authoritative pending list is the persisted records, not a detached count.
        pending_rows = pending_review_items(self._db, self.state.run_id)
        pending_ids = [r["transaction_id"] for r in pending_rows]
        pending_count = len(pending_rows)
        decisions_applied = int(h.get("decisions_applied") or h.get("total_reviewed") or 0)
        ids_line = ", ".join(pending_ids) if pending_ids else "None"
        if pending_count and ids_line == "None":
            ids_line = ", ".join(pending_ids)
        human_section = (
            f"- Decisions Applied: {decisions_applied}\n"
            f"- Pending Human Count: {pending_count}\n"
            f"- Item IDs: {ids_line}"
        )
        report = f"""# AI Finance Controller — Run Report

## 1. Executive Summary
Run **{self.state.run_id}** completed in **{self.state.ar_ap_mode}** mode.
Match rate **{m.get('match_rate', 0):.1%}**, accuracy **{m.get('accuracy', 0):.1%}**, F1 **{m.get('f1', 0):.1%}**.

## 2. Process Mode
- User request: {self.state.user_request}
- Confirmation: {self.state.confirmation_message}
- Mode: {self.state.ar_ap_mode}

## 3. Ingestion
{self.state.raw_records_summary}

## 4. Reconciliation Results
{p}

## 5. Exception Analysis
{self.state.investigation_summary}

## 6. Human Review Decisions
{human_section}

## 7. Journal Entries & ERP Write-Back
{j}

## 8. Evaluation Metrics
- Accuracy: {m.get('accuracy')}
- Precision: {m.get('precision')}
- Recall: {m.get('recall')}
- F1: {m.get('f1')}
- Match rate: {m.get('match_rate')}
- False-match rate: {m.get('false_match_rate')}
- Confusion: TP={m.get('tp')} FP={m.get('fp')} FN={m.get('fn')} TN={m.get('tn')}

## 9. Key Insights
- Auto-match band (≥0.90): {self.state.auto_match_count}
- Review band (0.70–0.89): {self.state.review_band_count}
- Exception band (<0.70): {self.state.exception_count}
- Candidates: {self.state.candidates_summary.get('total_candidates')}

## 10. Database Schema
Persisted to `data/finance_controller.db` (source_transactions, match_candidates, exceptions, human_reviews, audit_log, reconciliation_results, journal_entries, erp_writeback_log, evaluation_metrics, flow_runs).

## 11. How to Run
```bash
python scripts/run_finance_flow.py
# or POST /api/reconcile with use_crew_flow=true
```
"""
        self.state.final_report = report
        summary = {
            "run_id": self.state.run_id,
            "ar_ap_mode": self.state.ar_ap_mode,
            "evaluation": {k: m.get(k) for k in ("accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate")},
            "persistence": p,
            "journals": j,
            "auto_match_count": self.state.auto_match_count,
            "review_band_count": self.state.review_band_count,
            "exception_count": self.state.exception_count,
            "pending_human_count": pending_count,
            "pending_review_items": pending_rows,
            "human_review": {
                **h,
                "pending_human_count": pending_count,
                "item_ids": pending_ids,
                "decisions_applied": decisions_applied,
            },
        }
        self._db.finish_run(self.state.run_id, summary, report, status="completed")
        self._stage("summary_report", "completed", {"report_chars": len(report)})
        return report


def run_finance_flow(
    user_request: str = "Run AR reconciliation on demo feeds",
    ar_ap_mode: str = "AR",
    bank_csv_path: str | None = None,
    payment_processor_csv_path: str | None = None,
    ground_truth_csv_path: str | None = None,
    bank_pdf_path: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    import logging

    log = logging.getLogger(__name__)
    os.environ["CREWAI_TRACING_ENABLED"] = "false"
    flow = AIFinanceController(db_path=db_path)
    inputs = {
        "user_request": user_request,
        "ar_ap_mode": ar_ap_mode,
    }
    if bank_csv_path:
        inputs["bank_csv_path"] = bank_csv_path
    if payment_processor_csv_path:
        inputs["payment_processor_csv_path"] = payment_processor_csv_path
    if ground_truth_csv_path:
        inputs["ground_truth_csv_path"] = ground_truth_csv_path
    if bank_pdf_path:
        inputs["bank_pdf_path"] = bank_pdf_path

    log.info(
        "CREWAI_KICKOFF AIFinanceController inputs=%s",
        {k: (str(v)[:120] if isinstance(v, str) else v) for k, v in inputs.items()},
    )
    report = flow.kickoff(inputs=inputs)
    log.info(
        "CREWAI_KICKOFF_DONE run_id=%s stages=%s eval=%s",
        flow.state.run_id,
        [s.get("name") for s in (flow.state.stages or [])],
        {k: flow.state.evaluation_metrics.get(k) for k in ("match_rate", "accuracy", "f1")},
    )
    return {
        "run_id": flow.state.run_id,
        "ar_ap_mode": flow.state.ar_ap_mode,
        "stages": flow.state.stages,
        "confirmation_message": flow.state.confirmation_message,
        "evaluation_metrics": flow.state.evaluation_metrics,
        "persistence_summary": flow.state.persistence_summary,
        "journal_entries_summary": flow.state.journal_entries_summary,
        "investigation_summary": flow.state.investigation_summary,
        "human_review_summary": flow.state.human_review_summary,
        "final_report": flow.state.final_report or str(report),
        "db_path": flow.state._db_path,
    }
