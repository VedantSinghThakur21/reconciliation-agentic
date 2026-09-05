from __future__ import annotations

"""
Intent classifier for the dashboard assistant (legacy /api/assistant/classify).

Workspace AG-UI planning lives in src.agui.planner (LLM-only, no keyword rules).
This module keeps the older single-label classifier for non-AG-UI assistant routes.
"""

import json
import logging
import os
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

INTENTS = (
    "attention",
    "exceptions",
    "high_confidence_matches",
    "reconcile",
    "investigate",
    "approve",
    "reject",
    "unknown",
)

UI_INTENT_MAP = {
    "attention": "attention",
    "exceptions": "exceptions",
    "high_confidence_matches": "high_confidence",
    "high_confidence": "high_confidence",
    "reconcile": "reconcile",
    "investigate": "investigate",
    "approve": "approve",
    "reject": "reject",
    "unknown": "unknown",
}

TXN_RE = re.compile(r"\b((?:BNK|ERP|TXN|PAY|INV|PP)[-_][A-Z0-9]+)\b", re.I)

SYSTEM_PROMPT = """You classify short finance-ops assistant queries for a reconciliation demo.

Return ONLY valid JSON with keys:
  "intent": one of """ + json.dumps(list(INTENTS)) + """
  "txn_ref": string or null (transaction id like BNK-2101 / ERP-1021 when present)

Intent meanings:
- attention: what needs human attention / open reviews / blockers
- exceptions: show the exception / HITL queue
- high_confidence_matches: show strong auto matches
- reconcile: run reconciliation / kick off matching
- investigate: inspect one transaction's match scores (needs txn_ref)
- approve: approve / confirm a pending exception (needs txn_ref)
- reject: reject a pending exception (needs txn_ref)
- unknown: anything else

Extract txn_ref from the text when investigate/approve/reject. Uppercase the id.
Do not invent a txn_ref if none appears.
Interpret semantically — do not require exact catchphrases.
"""


def extract_txn_ref(text: str) -> str | None:
    m = TXN_RE.search(text or "")
    return m.group(1).upper().replace("_", "-") if m else None


def _normalize(intent: str, txn_ref: str | None, source: str) -> dict[str, Any]:
    raw = (intent or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    if raw in ("high_confidence_match", "highconfidence", "high_conf"):
        raw = "high_confidence_matches"
    ui = UI_INTENT_MAP.get(raw, "unknown")
    ref = (txn_ref or "").strip().upper() or None
    if ref:
        ref = ref.replace("_", "-")
    return {
        "intent": ui,
        "txn_ref": ref,
        "llm_intent": raw if raw in INTENTS else raw,
        "source": source,
    }


def classify_intent(text: str) -> dict[str, Any]:
    """Classify free text → {intent, txn_ref, source, llm_intent}. LLM-first."""
    q = (text or "").strip()
    if not q:
        return _normalize("unknown", None, "empty")

    extracted = extract_txn_ref(q)

    if os.getenv("GEMINI_API_KEY"):
        try:
            result = _classify_gemini(q)
            if not result.get("txn_ref"):
                result["txn_ref"] = extracted
            out = _normalize(result.get("intent") or "unknown", result.get("txn_ref"), "gemini")
            if out["intent"] in ("investigate", "approve", "reject") and not out["txn_ref"]:
                out["txn_ref"] = extracted
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("Gemini classify failed: %s", e)
            if (os.getenv("LLM_PROVIDER") or "").strip().lower() in ("gemini", "google"):
                return _normalize("unknown", extracted, "llm_error")

    if os.getenv("OPENAI_API_KEY") and (os.getenv("LLM_PROVIDER") or "").strip().lower() != "gemini":
        try:
            result = _classify_openai(q)
            if not result.get("txn_ref"):
                result["txn_ref"] = extracted
            out = _normalize(result.get("intent") or "unknown", result.get("txn_ref"), "openai")
            if out["intent"] in ("investigate", "approve", "reject") and not out["txn_ref"]:
                out["txn_ref"] = extracted
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAI classify failed: %s", e)

    # No keyword fallback — unknown if LLM unavailable/fails
    return _normalize("unknown", extracted, "no_llm")


def classify_workspace_intent(text: str) -> dict[str, Any]:
    """
    Compatibility wrapper → LLM workspace planner (no keyword rules).

    Prefer calling src.agui.planner.plan_workspace directly from AG-UI agent.
    """
    from src.agui.planner import plan_workspace

    plan = plan_workspace(text)
    return {
        "intent": plan.intent.primary,
        "secondary": list(plan.intent.secondary),
        "txn_ref": plan.txn_ref,
        "source": plan.source,
        "reasoning": plan.reasoning,
        "confidence": plan.confidence,
        "run_reconciliation": plan.actions.run_reconciliation,
        "message": plan.actions.clarification_question,
        "ui_components": list(plan.ui.components),
        "title": plan.ui.title,
    }


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _classify_gemini(text: str) -> dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY") or ""
    model = os.getenv("GEMINI_MODEL") or "gemini-flash-latest"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={key}"
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": SYSTEM_PROMPT + "\n\nUser query:\n" + text}],
            }
        ],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    last_err: Exception | None = None
    for attempt in range(3):
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            parts = (
                payload.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])
            )
            text_out = parts[0].get("text") if parts else ""
            return _parse_json_object(text_out or "{}")
        except HTTPError as e:
            last_err = e
            if e.code in (429, 503) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    assert last_err is not None
    raise last_err


def _classify_openai(text: str) -> dict[str, Any]:
    key = os.getenv("OPENAI_API_KEY") or ""
    model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return _parse_json_object(content)
