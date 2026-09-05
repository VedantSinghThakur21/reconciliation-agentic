from __future__ import annotations
"""
Mock LLM provider — offline testing without API keys.

For live Gemini/OpenAI, set LLM_PROVIDER and the corresponding API key in .env
and extend factory.py (not included by default in this extract).
"""

import json
import logging
import re
from typing import Any

from .base import BaseLLMProvider

logger = logging.getLogger(__name__)


class MockProvider(BaseLLMProvider):
    def provider_name(self) -> str:
        return "mock"

    async def generate_json(
        self,
        prompt: str,
        schema: dict,
        purpose: str = "general",
        tenant_id: str | None = None,
    ) -> list[dict]:
        logger.info("[MockProvider] purpose=%s prompt_len=%d", purpose, len(prompt))

        props = (schema or {}).get("properties") or {}
        if "matchType" in props or "matches" in props or "decisions" in props:
            return self._recon_stub(prompt, schema)

        return [self._mock_from_schema(schema)]

    def _recon_stub(self, prompt: str, schema: dict) -> list[dict]:
        """
        Offline heuristic AI matcher:
        - Prefer amount-corroborated invoice suggestions for leftover payments
        - Always route as manual_review / pending_review (never auto-approve)
        """
        props = (schema or {}).get("properties") or {}

        # Schema style used by ai_matcher nodes: decisions[] with matchType
        if "decisions" in props or '"matchType"' in json.dumps(schema):
            decisions = self._build_decisions(prompt)
            if decisions:
                return [{"decisions": decisions, "allocationSummary": "Mock AI heuristic review suggestions."}]
            return [{
                "decisions": [],
                "allocationSummary": "MockProvider: insufficient evidence for AI match.",
            }]

        inv_ids = re.findall(r"INV-[A-Z0-9-]+", prompt, flags=re.IGNORECASE)
        pay_ids = re.findall(r"(?:PAY|TXN|PP)-[A-Z0-9-]+", prompt, flags=re.IGNORECASE)
        if inv_ids and pay_ids:
            return [{
                "matches": [{
                    "paymentId": pay_ids[0],
                    "invoiceId": inv_ids[0],
                    "status": "pending_review",
                    "confidence": 0.62,
                    "reasoning": "Mock AI: amount/name corroboration suggests review candidate.",
                    "amountApplied": 0.0,
                    "matchType": "manual_review",
                }]
            }]
        return [{
            "matches": [],
            "reasoning": "MockProvider: no confident match (offline stub).",
        }]

    def _build_decisions(self, prompt: str) -> list[dict]:
        """Parse payment/invoice ids and amounts from the prompt; propose reviews."""
        # payment blocks often include id + amount
        pay_blocks = re.findall(
            r"(?:paymentId|Payment ID|id)[\"':\s]+((?:PAY|TXN|PP)-[A-Z0-9-]+).*?(?:amount|Amount)[\"':\s]+([0-9]+(?:\.[0-9]+)?)",
            prompt,
            flags=re.I | re.S,
        )
        inv_blocks = re.findall(
            r"(INV-[A-Z0-9-]+).*?(?:amount|balance|Amount|Balance)[\"':\s]+([0-9]+(?:\.[0-9]+)?)",
            prompt,
            flags=re.I | re.S,
        )
        if not pay_blocks:
            pay_ids = re.findall(r"(?:PAY|TXN|PP)-[A-Z0-9-]+", prompt, flags=re.I)
            pay_blocks = [(p, "0") for p in dict.fromkeys(pay_ids)]
        if not inv_blocks:
            inv_ids = re.findall(r"INV-[A-Z0-9-]+", prompt, flags=re.I)
            inv_blocks = [(i, "0") for i in dict.fromkeys(inv_ids)]

        decisions = []
        used_inv: set[str] = set()
        for pay_id, pay_amt_s in pay_blocks:
            pay_amt = float(pay_amt_s or 0)
            best = None
            best_delta = float("inf")
            for inv_id, inv_amt_s in inv_blocks:
                if inv_id in used_inv:
                    continue
                inv_amt = float(inv_amt_s or 0)
                delta = abs(inv_amt - pay_amt) if pay_amt and inv_amt else 999999
                # Prefer closer amounts; also allow partials (payment < invoice)
                if inv_amt and pay_amt and pay_amt <= inv_amt + 0.01 and delta < best_delta:
                    best = (inv_id, inv_amt, delta)
                    best_delta = delta
            if best and best_delta < max(pay_amt * 0.6, 1.0):
                inv_id, inv_amt, delta = best
                used_inv.add(inv_id)
                conf = 0.72 if delta < 0.05 else 0.58
                decisions.append({
                    "paymentId": pay_id,
                    "matchType": "manual_review",
                    "targetInvoiceIds": [inv_id],
                    "confidence": conf,
                    "reasoning": (
                        f"Mock AI agent: payment {pay_id} (₹{pay_amt:,.2f}) corroborates "
                        f"invoice {inv_id} (₹{inv_amt:,.2f}); routed for human review."
                    ),
                    "amountApplied": min(pay_amt, inv_amt) if pay_amt and inv_amt else 0.0,
                })
            else:
                decisions.append({
                    "paymentId": pay_id,
                    "matchType": "no_match",
                    "targetInvoiceIds": [],
                    "confidence": 0.0,
                    "reasoning": f"Mock AI agent: no corroborated invoice for {pay_id}.",
                    "amountApplied": 0.0,
                })
        return decisions

    def _mock_from_schema(self, schema: dict, key_name: str = "") -> Any:
        s_type = schema.get("type", "string")
        if s_type == "object":
            return {
                k: self._mock_from_schema(v, k)
                for k, v in (schema.get("properties") or {}).items()
            }
        if s_type == "array":
            item = schema.get("items", {"type": "string"})
            return [self._mock_from_schema(item, key_name)]
        if s_type in ("number", "integer"):
            return 0.55 if "confidence" in key_name.lower() else 0.0
        if s_type == "boolean":
            return False
        if "enum" in schema:
            enums = schema["enum"]
            return enums[0] if enums else "mock"
        k = key_name.lower()
        if "customer" in k or "vendor" in k or "sender" in k:
            return "Acme Corp"
        if "date" in k:
            return "2026-06-01"
        if "reason" in k:
            return "Mock offline response."
        return f"mock_{key_name}" if key_name else "mock"

    async def generate_text(
        self,
        prompt: str,
        purpose: str = "general",
        tenant_id: str | None = None,
    ) -> str:
        return "Mock response from MockProvider."
