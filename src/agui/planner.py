"""LLM workspace planner — semantic intent + UI component selection (no keyword rules).

Primary provider for this module: Groq (OpenAI-compatible).
Optional fallback: Gemini. Optional tertiary: OpenAI.
Does not use src.llm MockProvider (matching-engine abstraction).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, field_validator

from src.agui.ui_schema import ALLOWED_COMPONENT_TYPES

logger = logging.getLogger(__name__)

PRIMARY_CAPABILITIES = (
    "reconciliation",
    "exception_review",
    "amount_mismatch",
    "transaction_investigation",
    "finance_summary",
    "cash_position",
    "audit_review",
    "clarification",
)

TXN_RE = re.compile(r"\b((?:BNK|ERP|TXN|PAY|INV|PP)[-_][A-Z0-9]+)\b", re.I)

_disabled_providers: set[str] = set()

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"

PLANNER_SYSTEM = f"""You are the ReconIQ finance workspace planner.

Interpret the user's natural-language request semantically. Do NOT rely on fixed catchphrases.
Infer what they want done and what they want to see.

Return ONLY valid JSON with this shape:
{{
  "intent": {{
    "primary": one of {json.dumps(list(PRIMARY_CAPABILITIES))},
    "secondary": [zero or more of those same labels, excluding primary]
  }},
  "reasoning": "short explanation of how you interpreted the request",
  "confidence": number from 0 to 1,
  "actions": {{
    "run_reconciliation": boolean,
    "needs_clarification": boolean,
    "clarification_question": string or null
  }},
  "txn_ref": string or null,
  "ui": {{
    "title": "workspace title",
    "layout": "dashboard" | "focused" | "detail" | "table",
    "components": [subset of {json.dumps(sorted(ALLOWED_COMPONENT_TYPES - {"clarification", "unavailable", "agent_progress"}))}]
  }}
}}

Rules:
- primary is the main job. secondary are additional views they also asked for (or clearly implied).
- Set run_reconciliation=true only when they want matching/reconciliation executed (not merely to view past results).
- For investigation of a single transaction, set primary=transaction_investigation and extract txn_ref when present.
- If they ask to approve/reject/write-off in chat, set needs_clarification=true and explain that human decisions must be clicked in the UI (do not treat chat as a recorded decision).
- If the request is ambiguous or outside finance reconciliation, set primary=clarification and needs_clarification=true.
- Choose ui.components that best present the answer. Prefer fewer, relevant components over dumping everything.
- Choose ui.layout: dashboard (broad recon/summary), focused (single-purpose), detail (one transaction), table (list-heavy mismatches).
- Never invent numeric results; you only choose structure. The backend fills real data.
- Do not emit JSX/HTML. components are type names only.
"""


class IntentSpec(BaseModel):
    primary: str
    secondary: list[str] = Field(default_factory=list)

    @field_validator("primary")
    @classmethod
    def _primary(cls, v: str) -> str:
        raw = (v or "clarification").strip().lower().replace("-", "_").replace(" ", "_")
        if raw not in PRIMARY_CAPABILITIES:
            return "clarification"
        return raw

    @field_validator("secondary")
    @classmethod
    def _secondary(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v or []:
            raw = str(item).strip().lower().replace("-", "_").replace(" ", "_")
            if raw in PRIMARY_CAPABILITIES and raw not in out and raw != "clarification":
                out.append(raw)
        return out


class ActionSpec(BaseModel):
    run_reconciliation: bool = False
    needs_clarification: bool = False
    clarification_question: str | None = None


class UIPlanSpec(BaseModel):
    title: str = "Finance workspace"
    layout: str = "focused"
    components: list[str] = Field(default_factory=list)

    @field_validator("components")
    @classmethod
    def _components(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v or []:
            t = str(item).strip().lower()
            if t in ALLOWED_COMPONENT_TYPES and t not in out and t not in {"unavailable"}:
                out.append(t)
        return out


class WorkspacePlan(BaseModel):
    intent: IntentSpec
    reasoning: str = ""
    confidence: float = 0.0
    actions: ActionSpec = Field(default_factory=ActionSpec)
    txn_ref: str | None = None
    ui: UIPlanSpec = Field(default_factory=UIPlanSpec)
    source: str = "llm"

    def topics(self) -> list[str]:
        items = [self.intent.primary, *self.intent.secondary]
        seen: set[str] = set()
        out: list[str] = []
        for t in items:
            if t not in seen and t != "clarification":
                seen.add(t)
                out.append(t)
        return out


def extract_txn_ref(text: str) -> str | None:
    m = TXN_RE.search(text or "")
    return m.group(1).upper().replace("_", "-") if m else None


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _llm_available() -> bool:
    if os.getenv("GROQ_API_KEY") and "groq" not in _disabled_providers:
        return True
    if os.getenv("GEMINI_API_KEY") and "gemini" not in _disabled_providers:
        return True
    if os.getenv("OPENAI_API_KEY") and "openai" not in _disabled_providers:
        return True
    return bool(os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _chat_completions(
    user_text: str,
    *,
    url: str,
    api_key: str,
    model: str,
    provider_name: str,
) -> dict[str, Any]:
    """OpenAI-compatible chat.completions → parsed JSON object."""
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": user_text},
        ],
    }
    delays = (1.0, 3.0, 8.0)
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays):
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                # Cloudflare on api.groq.com returns 403/1010 without a UA.
                "User-Agent": "Mozilla/5.0 (compatible; reconq-core-planner/0.1)",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            return _parse_json_object(content)
        except HTTPError as e:
            last_err = e
            if e.code == 401:
                _disabled_providers.add(provider_name)
                logger.warning("%s auth failed (%s) — disabling for this process", provider_name, e.code)
                raise
            if e.code in (429, 503) and attempt < len(delays) - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and str(retry_after).isdigit() else delay
                logger.warning("%s %s — retry in %.1fs", provider_name, e.code, wait)
                time.sleep(wait)
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    assert last_err is not None
    raise last_err


def _call_groq(user_text: str) -> dict[str, Any]:
    key = os.getenv("GROQ_API_KEY") or ""
    model = (os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL).strip()
    return _chat_completions(
        user_text,
        url=GROQ_CHAT_URL,
        api_key=key,
        model=model,
        provider_name="groq",
    )


def _call_openai(user_text: str) -> dict[str, Any]:
    key = os.getenv("OPENAI_API_KEY") or ""
    model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    return _chat_completions(
        user_text,
        url="https://api.openai.com/v1/chat/completions",
        api_key=key,
        model=model,
        provider_name="openai",
    )


def _call_gemini(user_text: str) -> dict[str, Any]:
    """Optional fallback — Gemini native generateContent API."""
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
                "parts": [{"text": PLANNER_SYSTEM + "\n\nUser request:\n" + user_text}],
            }
        ],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    delays = (2.0, 5.0, 12.0)
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays):
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
            text_out = parts[0].get("text") if parts else ""
            return _parse_json_object(text_out or "{}")
        except HTTPError as e:
            last_err = e
            if e.code in (429, 503) and attempt < len(delays) - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and str(retry_after).isdigit() else delay
                logger.warning("Gemini %s — retry in %.1fs", e.code, wait)
                time.sleep(wait)
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    assert last_err is not None
    raise last_err


def _provider_chain() -> list[tuple[str, Callable[[str], dict[str, Any]]]]:
    """Workspace planner order: Groq (primary) → Gemini (optional) → OpenAI (optional)."""
    chain: list[tuple[str, Callable[[str], dict[str, Any]]]] = []

    def add(name: str, fn: Callable[[str], dict[str, Any]], env_key: str) -> None:
        if name in _disabled_providers:
            return
        if not os.getenv(env_key):
            return
        if any(n == name for n, _ in chain):
            return
        chain.append((name, fn))

    add("groq", _call_groq, "GROQ_API_KEY")
    add("gemini", _call_gemini, "GEMINI_API_KEY")
    add("openai", _call_openai, "OPENAI_API_KEY")
    return chain


def _user_message_for_errors(errors: list[str]) -> str:
    blob = " | ".join(errors).lower()
    if "429" in blob or "too many requests" in blob:
        return (
            "The workspace planner LLM is rate-limited (HTTP 429). "
            "Wait a moment and try again, or check GROQ_API_KEY quota. "
            "No keyword fallback is used — planning requires a live LLM."
        )
    if "401" in blob or "unauthorized" in blob or "403" in blob:
        return (
            "LLM authentication failed (check GROQ_API_KEY in .env; "
            "optional GEMINI_API_KEY fallback). Update the key, restart the API, and retry."
        )
    return (
        "I couldn't reach an LLM to interpret your request. "
        "Check GROQ_API_KEY (primary) or GEMINI_API_KEY (fallback), then try again."
    )


def _normalize_plan(raw: dict[str, Any], *, user_text: str, source: str) -> WorkspacePlan:
    intent_raw = raw.get("intent") or {}
    if isinstance(intent_raw, str):
        intent_raw = {"primary": intent_raw, "secondary": []}
    actions_raw = raw.get("actions") or {}
    ui_raw = raw.get("ui") or {}

    plan = WorkspacePlan(
        intent=IntentSpec(
            primary=str(intent_raw.get("primary") or "clarification"),
            secondary=list(intent_raw.get("secondary") or []),
        ),
        reasoning=str(raw.get("reasoning") or ""),
        confidence=float(raw.get("confidence") or 0),
        actions=ActionSpec(
            run_reconciliation=bool(actions_raw.get("run_reconciliation")),
            needs_clarification=bool(actions_raw.get("needs_clarification")),
            clarification_question=actions_raw.get("clarification_question"),
        ),
        txn_ref=(str(raw["txn_ref"]).upper().replace("_", "-") if raw.get("txn_ref") else None),
        ui=UIPlanSpec(
            title=str(ui_raw.get("title") or "Finance workspace"),
            layout=str(ui_raw.get("layout") or "focused"),
            components=list(ui_raw.get("components") or []),
        ),
        source=source,
    )

    if not plan.txn_ref:
        plan.txn_ref = extract_txn_ref(user_text)

    if plan.intent.primary == "transaction_investigation" and not plan.txn_ref:
        plan.actions.needs_clarification = True
        plan.actions.clarification_question = (
            plan.actions.clarification_question
            or "Which transaction should I investigate? Include an id such as BNK-2101."
        )
        plan.intent.primary = "clarification"

    # Reconciliation as the primary job always executes the CrewAI flow — never reuse stale runs.
    if plan.intent.primary == "reconciliation" and not plan.actions.needs_clarification:
        plan.actions.run_reconciliation = True
        if "exception_table" not in plan.ui.components:
            plan.ui.components.append("exception_table")
        if "exception_review" not in plan.intent.secondary:
            plan.intent.secondary.append("exception_review")

    if plan.confidence < 0.45 and plan.intent.primary != "clarification":
        plan.actions.needs_clarification = True
        plan.actions.clarification_question = (
            plan.actions.clarification_question
            or "I'm not confident I understood that. Could you rephrase what you need in the finance workspace?"
        )
        plan.intent.primary = "clarification"

    if not plan.ui.components and plan.intent.primary != "clarification":
        plan.ui.components = _default_components_for_topics(plan.topics())

    return plan


def _default_components_for_topics(topics: list[str]) -> list[str]:
    mapping = {
        "reconciliation": ["metric_grid", "chart", "kpi", "exception_table", "recommendation"],
        "exception_review": ["kpi", "exception_table", "recommendation"],
        "amount_mismatch": ["kpi", "transaction_table", "comparison"],
        "transaction_investigation": [
            "transaction_detail",
            "comparison",
            "metric_grid",
            "recommendation",
        ],
        "finance_summary": ["metric_grid", "chart"],
        "cash_position": ["kpi"],
        "audit_review": ["kpi", "audit_timeline"],
    }
    out: list[str] = []
    for t in topics:
        for c in mapping.get(t, []):
            if c not in out:
                out.append(c)
    return out


def plan_workspace(user_text: str) -> WorkspacePlan:
    """
    Interpret natural language via LLM into a WorkspacePlan.

    Primary: Groq. Optional fallback: Gemini, then OpenAI.
    No keyword/phrase → intent rules.
    """
    q = (user_text or "").strip()
    if not q:
        return WorkspacePlan(
            intent=IntentSpec(primary="clarification", secondary=[]),
            reasoning="Empty request",
            confidence=0.0,
            actions=ActionSpec(
                needs_clarification=True,
                clarification_question="What would you like to do in the finance workspace?",
            ),
            ui=UIPlanSpec(title="Need a request", components=["clarification"]),
            source="empty",
        )

    if not _llm_available():
        return WorkspacePlan(
            intent=IntentSpec(primary="clarification", secondary=[]),
            reasoning="No LLM configured for semantic planning",
            confidence=0.0,
            actions=ActionSpec(
                needs_clarification=True,
                clarification_question=(
                    "Semantic workspace planning requires GROQ_API_KEY "
                    "(optional fallback: GEMINI_API_KEY). Configure an LLM, then retry."
                ),
            ),
            ui=UIPlanSpec(title="LLM required", components=["clarification"]),
            source="no_llm",
        )

    errors: list[str] = []
    for source, fn in _provider_chain():
        try:
            raw = fn(q)
            return _normalize_plan(raw, user_text=q, source=source)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}: {e}")
            logger.warning("Workspace planner %s failed: %s", source, e)
            continue

    detail = "; ".join(errors) if errors else "no provider"
    return WorkspacePlan(
        intent=IntentSpec(primary="clarification", secondary=[]),
        reasoning=f"Planner LLM error: {detail}",
        confidence=0.0,
        actions=ActionSpec(
            needs_clarification=True,
            clarification_question=_user_message_for_errors(errors),
        ),
        ui=UIPlanSpec(title="Planner unavailable", components=["clarification"]),
        source="llm_error",
    )
