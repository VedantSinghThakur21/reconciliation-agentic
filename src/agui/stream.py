"""AG-UI SSE progress stream for a CrewAI AMP kickoff (parallel to existing poll).

Uses official ag-ui-protocol EventEncoder + typed events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from src.agui.events import (
    encode_sse,
    run_error,
    run_finished,
    run_started,
    step_finished,
    step_started,
    text_message_content,
    text_message_end,
    text_message_start,
)

logger = logging.getLogger(__name__)

_PROGRESS_BY_STATE = {
    "PENDING": "Queued on CrewAI AMP…",
    "STARTED": "Investigating leftover transactions…",
    "RUNNING": "Scoring candidates and enriching matches…",
    "SUCCESS": "Enrichment complete.",
    "COMPLETED": "Enrichment complete.",
    "FAILED": "CrewAI run failed.",
    "ERROR": "CrewAI run failed.",
}


def _progress_text(payload: dict[str, Any]) -> str:
    state = str(payload.get("state") or "").upper()
    status = str(payload.get("status") or "")
    last_task = payload.get("last_executed_task") or payload.get("last_step")
    if last_task:
        return f"Working: {last_task}"
    if state in _PROGRESS_BY_STATE:
        return _PROGRESS_BY_STATE[state]
    if status:
        return status
    return "CrewAI enrichment in progress…"


def _terminal(payload: dict[str, Any]) -> str | None:
    state = str(payload.get("state") or "").upper()
    status = str(payload.get("status") or "").lower()
    if state in {"SUCCESS", "COMPLETED", "SUCCEEDED", "DONE"} or "success" in status or "completed" in status:
        return "success"
    if state in {"FAILED", "ERROR", "CANCELLED", "CANCELED"} or "fail" in status or "error" in status:
        return "failure"
    return None


async def stream_kickoff_progress(
    kickoff_id: str,
    *,
    poll_interval_sec: float | None = None,
    max_wait_sec: float | None = None,
) -> AsyncIterator[str]:
    """
    Yield AG-UI SSE frames while polling AMP status.

    Does not replace `CrewAIClient.wait()` — UI/API polling remains source of truth.
    """
    from src.agents.crewai_client import CrewAIClient, CrewAIClientError, crewai_configured

    interval = poll_interval_sec or float(os.getenv("CREWAI_POLL_INTERVAL_SEC") or "5")
    max_wait = max_wait_sec or float(os.getenv("CREWAI_MAX_WAIT_SEC") or "600")
    thread_id = f"amp-{kickoff_id[:8]}"
    run_id = kickoff_id
    message_id = f"msg_{kickoff_id[:8]}"

    yield encode_sse(run_started(thread_id=thread_id, run_id=run_id))
    yield encode_sse(step_started("crewai_amp_wait"))
    yield encode_sse(text_message_start(message_id=message_id))
    yield encode_sse(
        text_message_content(
            message_id=message_id,
            delta="Connecting to CrewAI enrichment…\n",
        )
    )

    if not crewai_configured():
        yield encode_sse(run_error(message="CrewAI not configured", code="not_configured"))
        return

    client = CrewAIClient()
    started = time.time()
    last_text = ""
    last_state = ""

    try:
        while time.time() - started < max_wait:
            try:
                _http, _raw, payload = await asyncio.to_thread(client.status, kickoff_id, retries=2)
            except CrewAIClientError as e:
                soft = f"Waiting for AMP status ({e})…\n"
                if soft != last_text:
                    yield encode_sse(text_message_content(message_id=message_id, delta=soft))
                    last_text = soft
                await asyncio.sleep(interval)
                continue

            state = str(payload.get("state") or "")
            text = _progress_text(payload) + "\n"
            if state != last_state and last_state:
                yield encode_sse(step_finished(f"amp_{last_state.lower() or 'phase'}"))
                yield encode_sse(step_started(f"amp_{state.lower() or 'phase'}"))
            last_state = state

            if text != last_text:
                yield encode_sse(text_message_content(message_id=message_id, delta=text))
                last_text = text

            kind = _terminal(payload)
            if kind == "success":
                yield encode_sse(step_finished("crewai_amp_wait"))
                yield encode_sse(text_message_content(message_id=message_id, delta="Done.\n"))
                yield encode_sse(text_message_end(message_id=message_id))
                yield encode_sse(
                    run_finished(
                        thread_id=thread_id,
                        run_id=run_id,
                        result={"state": payload.get("state"), "kickoff_id": kickoff_id},
                    )
                )
                return
            if kind == "failure":
                yield encode_sse(
                    run_error(
                        message=str(payload.get("status") or payload.get("state") or "failed"),
                        code="amp_failed",
                    )
                )
                return

            await asyncio.sleep(interval)

        yield encode_sse(run_error(message="Timed out waiting for CrewAI", code="timeout"))
    except Exception as e:  # noqa: BLE001
        logger.exception("AG-UI stream error kickoff_id=%s", kickoff_id)
        yield encode_sse(run_error(message=str(e), code="stream_error"))
