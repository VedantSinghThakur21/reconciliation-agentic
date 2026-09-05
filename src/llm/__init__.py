from __future__ import annotations
"""LLM provider factory for reconq-core (defaults to mock)."""

import os

from .base import BaseLLMProvider
from .mock_provider import MockProvider

_provider: BaseLLMProvider | None = None


def get_llm_provider(tenant_id: str | None = None) -> BaseLLMProvider:
    """
    Return the configured LLM provider.

    reconq-core ships with MockProvider only. Set LLM_PROVIDER=mock (default).
    Live providers intentionally omitted from this public extract.
    """
    global _provider
    if _provider is None:
        name = (os.getenv("LLM_PROVIDER") or "mock").strip().lower()
        if name != "mock":
            # Fall back safely rather than requiring API keys for the hackathon extract.
            _provider = MockProvider()
        else:
            _provider = MockProvider()
    return _provider


def reset_llm_provider() -> None:
    global _provider
    _provider = None
