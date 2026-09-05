"""Controlled UI instruction schema — agent may only emit whitelisted component types."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ALLOWED_COMPONENT_TYPES = frozenset(
    {
        "kpi",
        "metric_grid",
        "transaction_table",
        "exception_table",
        "chart",
        "transaction_detail",
        "comparison",
        "recommendation",
        "agent_progress",
        "audit_timeline",
        "clarification",
        "unavailable",
    }
)


class IntentObject(BaseModel):
    primary: str
    secondary: list[str] = Field(default_factory=list)


class UIComponent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _whitelist(cls, v: str) -> str:
        if v not in ALLOWED_COMPONENT_TYPES:
            raise ValueError(f"Unknown component type rejected: {v}")
        return v


class WorkspaceUI(BaseModel):
    """Structured UI instructions consumed by the React component registry."""

    intent: IntentObject | str
    title: str
    status: Literal["ok", "unavailable", "clarification", "error", "agent_failure"] = "ok"
    message: str | None = None
    reasoning: str | None = None
    components: list[UIComponent] = Field(default_factory=list)
    run_id: str | None = None
    txn_ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    layout: str | None = None

    def as_state(self) -> dict[str, Any]:
        payload = self.model_dump()
        # Normalize intent to object form for the frontend
        if isinstance(payload.get("intent"), str):
            payload["intent"] = {"primary": payload["intent"], "secondary": []}
        data = payload.get("data") or {}
        return {
            "workspace": payload,
            "intent": payload["intent"],
            "reasoning": payload.get("reasoning"),
            "data": data,
            "ui": {"components": payload.get("components") or [], "title": payload.get("title")},
            "pending_human_count": data.get("pending_human_count", 0),
            "pending_review_items": data.get("pending_review_items") or [],
            "exceptions": data.get("exceptions") or [],
            "reconciliation_summary": data.get("reconciliation_summary") or {},
        }
