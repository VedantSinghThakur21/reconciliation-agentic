from __future__ import annotations

"""
Optional CrewAI AMP client for leftover enrichment.

Credentials (never hardcode):
  CREWAI_API_URL
  CREWAI_BEARER_TOKEN
  CREWAI_USER_BEARER_TOKEN  (optional)

Contract:
  POST {CREWAI_API_URL}/kickoff  {"inputs": {...}} → {"kickoff_id": "..."}
  GET  {CREWAI_API_URL}/status/{kickoff_id}

Fail-open: callers should catch CrewAIClientError / treat None returns as skip.
"""

import json
import logging
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# AMP jobs routinely take several minutes; 90s was silently timing out.
DEFAULT_MAX_WAIT_SEC = float(os.getenv("CREWAI_MAX_WAIT_SEC") or "600")
DEFAULT_POLL_INTERVAL_SEC = float(os.getenv("CREWAI_POLL_INTERVAL_SEC") or "5")

_SUCCESS_TOKENS = (
    "completed",
    "complete",
    "success",
    "succeeded",
    "done",
    "finished",
)
_FAILURE_TOKENS = ("failed", "error", "cancelled", "canceled")


class CrewAIClientError(RuntimeError):
    """Raised for AMP transport / protocol failures (never used to crash the pipeline)."""


def _env_url() -> str:
    # Prefer canonical names; accept legacy CREWAI_AMP_* during local migration
    return (os.getenv("CREWAI_API_URL") or os.getenv("CREWAI_AMP_URL") or "").rstrip("/")


def _env_bearer() -> str:
    return os.getenv("CREWAI_BEARER_TOKEN") or os.getenv("CREWAI_AMP_BEARER_TOKEN") or ""


def _env_user_bearer() -> str:
    return os.getenv("CREWAI_USER_BEARER_TOKEN") or os.getenv("CREWAI_AMP_USER_BEARER_TOKEN") or ""


def crewai_configured() -> bool:
    return bool(_env_url() and _env_bearer())


def _terminal_kind(payload: dict[str, Any]) -> str | None:
    """
    Return 'success' | 'failure' | None (still running).

    AMP historically returns state=STARTED|RUNNING|COMPLETED|FAILED (and sometimes
    a human `status` string). Be liberal: substring match, either field.
    """
    candidates = [
        str(payload.get("state") or ""),
        str(payload.get("status") or ""),
        str(payload.get("run_status") or ""),
    ]
    joined = " ".join(candidates).lower()
    if not joined.strip():
        return None
    if any(tok in joined for tok in _FAILURE_TOKENS):
        return "failure"
    if any(tok in joined for tok in _SUCCESS_TOKENS):
        return "success"
    return None


class CrewAIClient:
    """Thin env-gated AMP client. Safe to construct only when configured() is True."""

    def __init__(
        self,
        base_url: str | None = None,
        bearer_token: str | None = None,
        user_bearer_token: str | None = None,
        timeout_sec: float = 60.0,
    ) -> None:
        self.base_url = (base_url or _env_url()).rstrip("/")
        self.bearer_token = bearer_token or _env_bearer()
        self.user_bearer_token = user_bearer_token if user_bearer_token is not None else _env_user_bearer()
        self.timeout_sec = timeout_sec
        if not self.base_url or not self.bearer_token:
            raise CrewAIClientError(
                "CREWAI_API_URL and CREWAI_BEARER_TOKEN must be set to use CrewAI enrichment"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_raw(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, str, dict[str, Any] | list[Any] | str | None]:
        """Return (http_status, raw_body, parsed_json_or_None)."""
        url = f"{self.base_url}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=self.timeout_sec) as resp:
                http_status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read().decode("utf-8")
                if not raw:
                    return int(http_status), "", {}
                try:
                    return int(http_status), raw, json.loads(raw)
                except json.JSONDecodeError as e:
                    raise CrewAIClientError(
                        f"Non-JSON response from {path} (HTTP {http_status}): {raw[:500]}"
                    ) from e
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise CrewAIClientError(
                f"{method} {path} failed (HTTP {e.code}): {body[:500]}"
            ) from e
        except URLError as e:
            raise CrewAIClientError(f"{method} {path} network error: {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any] | str:
        _status, _raw, parsed = self._request_raw(method, path, payload)
        if parsed is None:
            return {}
        return parsed  # type: ignore[return-value]

    def get_inputs(self) -> dict[str, Any] | list[Any] | str:
        return self._request("GET", "/inputs")

    def kickoff(self, inputs: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {"inputs": {k: "" if v is None else str(v) for k, v in inputs.items()}}
        if meta:
            body["meta"] = meta
        result = self._request("POST", "/kickoff", body)
        if not isinstance(result, dict) or not result.get("kickoff_id"):
            raise CrewAIClientError(f"Unexpected kickoff response: {result!r}")
        return str(result["kickoff_id"])

    def status(self, kickoff_id: str, *, retries: int = 5) -> tuple[int, str, dict[str, Any]]:
        """
        GET /status/{kickoff_id} with brief retries on transient 404/5xx.

        Returns (http_status, raw_body, parsed_dict).
        """
        kid = kickoff_id.strip()
        last_err: Exception | None = None
        for attempt in range(max(retries, 1)):
            try:
                http_status, raw, parsed = self._request_raw("GET", f"/status/{kid}")
                if isinstance(parsed, dict):
                    return http_status, raw, parsed
                return http_status, raw, {"raw": parsed}
            except CrewAIClientError as e:
                last_err = e
                msg = str(e)
                if any(code in msg for code in ("(HTTP 404)", "(HTTP 429)", "(HTTP 502)", "(HTTP 503)", "(404)", "(429)", "(502)", "(503)")):
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise
        raise CrewAIClientError(f"Status unavailable for {kid}: {last_err}")

    def wait(
        self,
        kickoff_id: str,
        *,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
    ) -> dict[str, Any]:
        """Poll until terminal state or timeout (fail-open at call site)."""
        started = time.time()
        deadline = started + max_wait_sec
        attempt = 0
        last: dict[str, Any] = {}

        msg = (
            f"[CrewAI poll] start kickoff_id={kickoff_id} "
            f"max_wait_sec={max_wait_sec} poll_interval_sec={poll_interval_sec}"
        )
        print(msg, flush=True)
        logger.info(msg)

        # Brief pause so AMP can register the run before first status GET
        time.sleep(min(2.0, poll_interval_sec))

        while time.time() < deadline:
            attempt += 1
            elapsed = time.time() - started
            http_status = -1
            raw_body = ""
            try:
                http_status, raw_body, last = self.status(kickoff_id, retries=3)
            except CrewAIClientError as e:
                line = (
                    f"[CrewAI poll] attempt={attempt} elapsed={elapsed:.1f}s "
                    f"kickoff_id={kickoff_id} HTTP=ERR error={e}"
                )
                print(line, flush=True)
                logger.warning(line)
                time.sleep(poll_interval_sec)
                continue

            state_field = last.get("state")
            status_field = last.get("status")
            body_preview = raw_body if len(raw_body) <= 800 else raw_body[:800] + "...(truncated)"
            line = (
                f"[CrewAI poll] attempt={attempt} elapsed={elapsed:.1f}s "
                f"kickoff_id={kickoff_id} HTTP={http_status} "
                f"state={state_field!r} status={status_field!r} body={body_preview}"
            )
            print(line, flush=True)
            logger.info(line)

            kind = _terminal_kind(last)
            if kind == "success":
                done = (
                    f"[CrewAI poll] COMPLETED after attempt={attempt} "
                    f"elapsed={elapsed:.1f}s kickoff_id={kickoff_id}"
                )
                print(done, flush=True)
                logger.info(done)
                return last
            if kind == "failure":
                raise CrewAIClientError(f"CrewAI run failed: {last}")

            time.sleep(poll_interval_sec)

        elapsed = time.time() - started
        timeout_msg = (
            f"[CrewAI poll] TIMEOUT after attempt={attempt} elapsed={elapsed:.1f}s "
            f"(max_wait_sec={max_wait_sec}) kickoff_id={kickoff_id} last={last!r}"
        )
        print(timeout_msg, flush=True)
        logger.warning(timeout_msg)
        raise CrewAIClientError(
            f"Timed out waiting for kickoff {kickoff_id} after {elapsed:.1f}s "
            f"(max_wait_sec={max_wait_sec}); last payload={last!r}"
        )


def enrich_leftovers_with_crewai(
    leftovers: list[dict[str, Any]],
    *,
    user_request: str = "Enrich unmatched leftovers for AR reconciliation",
    ar_ap_mode: str = "AR",
    bank_csv_path: str = "",
    payment_processor_csv_path: str = "",
    ground_truth_csv_path: str = "",
    max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
) -> list[dict[str, Any]]:
    """
    Kick off AMP for leftover enrichment; return pending_review suggestion dicts.

    Returns [] on skip / any failure (fail-open). Never mutates matched balances.
    """
    if not leftovers:
        logger.info("CrewAI enrichment skipped: no leftovers")
        return []
    if not crewai_configured():
        logger.info("CrewAI enrichment skipped: CREWAI_API_URL / CREWAI_BEARER_TOKEN not set")
        return []

    try:
        client = CrewAIClient()
        inputs = {
            "user_request": user_request,
            "ar_ap_mode": ar_ap_mode,
            "bank_csv_path": bank_csv_path,
            "payment_processor_csv_path": payment_processor_csv_path,
            "ground_truth_csv_path": ground_truth_csv_path,
        }
        print(
            f"[CrewAI] kickoff starting leftovers={len(leftovers)} "
            f"max_wait_sec={max_wait_sec} inputs_keys={list(inputs.keys())}",
            flush=True,
        )
        kickoff_id = client.kickoff(inputs, meta={"source": "reconq-core-leftover-enrichment"})
        print(f"[CrewAI] kickoff OK id={kickoff_id}", flush=True)
        logger.info("CrewAI kickoff started id=%s leftovers=%d", kickoff_id, len(leftovers))
        status_payload = client.wait(kickoff_id, max_wait_sec=max_wait_sec)
        suggestions = _suggestions_from_amp(status_payload, leftovers)
        print(
            f"[CrewAI] enrichment mapped suggestions={len(suggestions)} "
            f"from status keys={list(status_payload.keys())}",
            flush=True,
        )
        return suggestions
    except CrewAIClientError as e:
        print(f"[CrewAI] enrichment failed open: {e}", flush=True)
        logger.warning("CrewAI enrichment failed open: %s", e)
        return []
    except Exception as e:  # noqa: BLE001 — never crash pipeline
        print(f"[CrewAI] enrichment unexpected error (fail-open): {e}", flush=True)
        logger.warning("CrewAI enrichment unexpected error (fail-open): %s", e)
        return []


def _suggestions_from_amp(
    status_payload: dict[str, Any],
    leftovers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map AMP result into pipeline pending_review suggestion records."""
    output = (
        status_payload.get("result")
        or status_payload.get("result_json")
        or status_payload.get("output")
        or status_payload.get("final_output")
    )
    suggestions: list[dict[str, Any]] = []

    # If AMP returns structured matches, prefer them
    parsed: Any = output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None

    if isinstance(parsed, dict):
        rows = parsed.get("pending_reviews") or parsed.get("suggestions") or parsed.get("matches") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                suggestions.append({
                    "paymentId": row.get("paymentId") or row.get("payment_id") or row.get("transaction_id"),
                    "invoiceId": row.get("invoiceId") or row.get("invoice_id") or row.get("matched_txn_id"),
                    "status": "pending_review",
                    "confidence": float(row.get("confidence") or 0.55),
                    "reasoning": str(row.get("reasoning") or row.get("explanation") or "CrewAI AMP suggestion"),
                    "amountApplied": float(row.get("amountApplied") or row.get("amount") or 0),
                    "reviewReason": str(row.get("reviewReason") or "crewai_enrichment"),
                    "source": "crewai_amp",
                })

    if suggestions:
        return [s for s in suggestions if s.get("paymentId")]

    # Fallback: annotate each leftover as CrewAI-reviewed pending item
    snippet = ""
    if isinstance(output, str):
        snippet = output[:400]
    elif output is not None:
        snippet = json.dumps(output, default=str)[:400]

    for p in leftovers:
        suggestions.append({
            "paymentId": p.get("id"),
            "invoiceId": None,
            "status": "pending_review",
            "confidence": 0.4,
            "reasoning": (
                "CrewAI AMP enrichment completed; no structured match payload — "
                f"routed to HITL. Output excerpt: {snippet or '(empty)'}"
            ),
            "amountApplied": 0.0,
            "reviewReason": "crewai_enrichment",
            "source": "crewai_amp",
        })
    return suggestions


# Backward-compatible aliases for older imports
CrewAIAmpError = CrewAIClientError
CrewAIAmpClient = CrewAIClient
amp_enabled = crewai_configured
run_amp_flow = None  # removed — use enrich_leftovers_with_crewai / CrewAIClient
