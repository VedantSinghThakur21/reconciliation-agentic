"""
Deprecated shim — use `src.agents.crewai_client` instead.

Kept so older imports (`src.finance_controller.crewai_amp`) keep working.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from src.agents.crewai_client import (
    CrewAIClient,
    CrewAIClientError,
    crewai_configured,
    enrich_leftovers_with_crewai,
)

logger = logging.getLogger(__name__)

warnings.warn(
    "src.finance_controller.crewai_amp is deprecated; import from src.agents.crewai_client",
    DeprecationWarning,
    stacklevel=2,
)

# Aliases
CrewAIAmpClient = CrewAIClient
CrewAIAmpError = CrewAIClientError
amp_enabled = crewai_configured


def run_amp_flow(
    *,
    user_request: str,
    ar_ap_mode: str = "AR",
    bank_csv_path: str | None = None,
    payment_processor_csv_path: str | None = None,
    ground_truth_csv_path: str | None = None,
    bank_pdf_path: str | None = None,
    poll: bool = True,
    max_wait_sec: float = 600.0,
) -> dict[str, Any]:
    """Compat wrapper around the consolidated CrewAIClient."""
    if not crewai_configured():
        raise CrewAIClientError("CREWAI_API_URL / CREWAI_BEARER_TOKEN not set")
    client = CrewAIClient()
    inputs = {
        "user_request": user_request,
        "ar_ap_mode": ar_ap_mode,
        "bank_csv_path": bank_csv_path or "",
        "payment_processor_csv_path": payment_processor_csv_path or "",
        "ground_truth_csv_path": ground_truth_csv_path or "",
    }
    if bank_pdf_path:
        inputs["bank_pdf_path"] = bank_pdf_path
    kickoff_id = client.kickoff(inputs, meta={"source": "finance_controller_shim"})
    out: dict[str, Any] = {
        "engine": "crewai-amp",
        "kickoff_id": kickoff_id,
        "inputs_sent": inputs,
        "status": "running",
    }
    if poll:
        status_payload = client.wait(kickoff_id, max_wait_sec=max_wait_sec)
        out["status"] = status_payload.get("state") or status_payload.get("status") or "completed"
        out["amp_result"] = status_payload
        out["final_output"] = (
            status_payload.get("result")
            or status_payload.get("result_json")
            or status_payload.get("output")
        )
    return out


__all__ = [
    "CrewAIAmpClient",
    "CrewAIAmpError",
    "amp_enabled",
    "run_amp_flow",
    "enrich_leftovers_with_crewai",
]
