from __future__ import annotations

"""Mocked AMP client + pipeline leftover enrichment tests (no real network)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.crewai_client import (
    CrewAIClient,
    CrewAIClientError,
    crewai_configured,
    enrich_leftovers_with_crewai,
)
from src.pipeline import run_pipeline


class _FakeResp:
    def __init__(self, payload: dict | str, status: int = 200):
        self._payload = payload
        self.status = status

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        if isinstance(self._payload, str):
            return self._payload.encode("utf-8")
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_crewai_configured_requires_both_env(monkeypatch):
    monkeypatch.delenv("CREWAI_API_URL", raising=False)
    monkeypatch.delenv("CREWAI_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CREWAI_AMP_URL", raising=False)
    monkeypatch.delenv("CREWAI_AMP_BEARER_TOKEN", raising=False)
    assert crewai_configured() is False
    monkeypatch.setenv("CREWAI_API_URL", "https://your-crewai-amp-deployment.example.com")
    assert crewai_configured() is False
    monkeypatch.setenv("CREWAI_BEARER_TOKEN", "test-token")
    assert crewai_configured() is True


def test_kickoff_and_status_mocked(monkeypatch):
    monkeypatch.setenv("CREWAI_API_URL", "https://your-crewai-amp-deployment.example.com")
    monkeypatch.setenv("CREWAI_BEARER_TOKEN", "test-token")

    calls: list[str] = []

    def fake_urlopen(req, timeout=60):
        url = req.full_url
        calls.append(f"{req.get_method()} {url}")
        if url.endswith("/kickoff"):
            return _FakeResp({"kickoff_id": "kid-abc"})
        if "/status/" in url:
            return _FakeResp({
                "state": "COMPLETED",
                "result": {
                    "pending_reviews": [
                        {
                            "paymentId": "PAY-X",
                            "invoiceId": "INV-X",
                            "confidence": 0.62,
                            "reasoning": "AMP mock suggestion",
                            "amountApplied": 100.0,
                        }
                    ]
                },
            })
        raise AssertionError(f"unexpected URL {url}")

    with patch("src.agents.crewai_client.urlopen", side_effect=fake_urlopen):
        client = CrewAIClient()
        kid = client.kickoff({"user_request": "test", "ar_ap_mode": "AR"})
        assert kid == "kid-abc"
        status = client.status(kid)
        assert status[0] == 200
        assert status[2]["state"] == "COMPLETED"

    assert any("/kickoff" in c for c in calls)
    assert any("/status/kid-abc" in c for c in calls)


def test_enrich_fail_open_on_5xx(monkeypatch):
    monkeypatch.setenv("CREWAI_API_URL", "https://your-crewai-amp-deployment.example.com")
    monkeypatch.setenv("CREWAI_BEARER_TOKEN", "test-token")

    from urllib.error import HTTPError
    from io import BytesIO

    def boom(req, timeout=60):
        raise HTTPError(
            req.full_url, 503, "Service Unavailable", hdrs=None, fp=BytesIO(b"down")
        )

    with patch("src.agents.crewai_client.urlopen", side_effect=boom):
        out = enrich_leftovers_with_crewai(
            [{"id": "PAY-1", "amount": 10}],
            max_wait_sec=1.0,
        )
    assert out == []


def test_pipeline_crewai_disabled_default():
    """Default path: no flag, no AMP attempt — valid StrictMatcher output."""
    result = run_pipeline(
        ROOT / "data" / "synthetic" / "sample_invoices.csv",
        ROOT / "data" / "synthetic" / "sample_payments.csv",
        use_crewai=False,
        use_ai_graph=False,
    )
    summary = result.summary()
    assert summary["total_decisions"] >= 3
    assert summary["auto_reconciled"] >= 2
    assert not any(d.get("source") == "crewai_amp" for d in result.decisions)


def test_pipeline_crewai_enabled_mocked(monkeypatch):
    monkeypatch.setenv("CREWAI_API_URL", "https://your-crewai-amp-deployment.example.com")
    monkeypatch.setenv("CREWAI_BEARER_TOKEN", "test-token")

    def fake_urlopen(req, timeout=60):
        url = req.full_url
        if url.endswith("/kickoff"):
            return _FakeResp({"kickoff_id": "kid-pipe"})
        if "/status/" in url:
            return _FakeResp({
                "state": "COMPLETED",
                "result": {
                    "pending_reviews": [
                        {
                            "paymentId": "PAY-UNMATCHED",
                            "invoiceId": None,
                            "confidence": 0.5,
                            "reasoning": "mock enrichment",
                            "amountApplied": 0,
                            "reviewReason": "crewai_enrichment",
                        }
                    ]
                },
            })
        raise AssertionError(url)

    # Force a leftover by patching enrich to see use_crewai path; also run real flow
    with patch("src.agents.crewai_client.urlopen", side_effect=fake_urlopen):
        with patch("src.agents.crewai_client.time.sleep", return_value=None):
            result = run_pipeline(
                ROOT / "data" / "synthetic" / "sample_invoices.csv",
                ROOT / "data" / "synthetic" / "sample_payments.csv",
                use_crewai=True,
                use_ai_graph=False,
                crewai_max_wait_sec=5.0,
            )

    assert result.summary()["total_decisions"] >= 3
    # If AMP returned structured suggestions for an unknown payment id, still valid output
    assert isinstance(result.decisions, list)
    assert len(result.review_queue.pending()) >= 1


def test_pipeline_crewai_flag_without_env_fail_open(monkeypatch):
    monkeypatch.delenv("CREWAI_API_URL", raising=False)
    monkeypatch.delenv("CREWAI_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CREWAI_AMP_URL", raising=False)
    monkeypatch.delenv("CREWAI_AMP_BEARER_TOKEN", raising=False)
    result = run_pipeline(
        ROOT / "data" / "synthetic" / "sample_invoices.csv",
        ROOT / "data" / "synthetic" / "sample_payments.csv",
        use_crewai=True,
        use_ai_graph=False,
    )
    assert result.summary()["total_decisions"] >= 3
    assert not any(d.get("source") == "crewai_amp" for d in result.decisions)
