"""AG-UI event helpers backed by the official ag-ui-protocol package."""

from __future__ import annotations

from typing import Any

from ag_ui.core import (
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

_encoder = EventEncoder(accept="text/event-stream")


def encode_sse(event: Any) -> str:
    """Encode an AG-UI event (typed model or dict) as an SSE frame."""
    if isinstance(event, dict):
        # Legacy callers may still pass dicts — wrap via encoder JSON path
        import json

        return f"data: {json.dumps(event, default=str)}\n\n"
    return _encoder.encode(event)


def run_started(*, thread_id: str, run_id: str, **extra: Any) -> RunStartedEvent:
    return RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
        **{k: v for k, v in extra.items() if k in ("timestamp",)},
    )


def run_finished(*, thread_id: str, run_id: str, result: Any = None) -> RunFinishedEvent:
    return RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id=thread_id,
        run_id=run_id,
        result=result,
    )


def run_error(*, message: str, code: str | None = None) -> RunErrorEvent:
    return RunErrorEvent(type=EventType.RUN_ERROR, message=message, code=code)


def step_started(step_name: str) -> StepStartedEvent:
    return StepStartedEvent(type=EventType.STEP_STARTED, step_name=step_name)


def step_finished(step_name: str) -> StepFinishedEvent:
    return StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=step_name)


def text_message_start(*, message_id: str | None = None) -> TextMessageStartEvent:
    import uuid

    mid = message_id or f"msg_{uuid.uuid4().hex[:10]}"
    return TextMessageStartEvent(
        type=EventType.TEXT_MESSAGE_START,
        message_id=mid,
        role="assistant",
    )


def text_message_content(*, message_id: str, delta: str) -> TextMessageContentEvent:
    return TextMessageContentEvent(
        type=EventType.TEXT_MESSAGE_CONTENT,
        message_id=message_id,
        delta=delta,
    )


def text_message_end(*, message_id: str) -> TextMessageEndEvent:
    return TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id)
