from __future__ import annotations
"""Abstract base class for LLM providers used by the AI semantic matcher."""

from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        schema: dict,
        purpose: str = "general",
        tenant_id: str | None = None,
    ) -> list[dict]:
        pass

    @abstractmethod
    async def generate_text(
        self,
        prompt: str,
        purpose: str = "general",
        tenant_id: str | None = None,
    ) -> str:
        pass

    async def generate_text_stream(
        self,
        prompt: str,
        purpose: str = "general",
        tenant_id: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate_text(prompt, purpose, tenant_id)

    @abstractmethod
    def provider_name(self) -> str:
        pass


class LLMProviderError(Exception):
    """Raised when an LLM provider fails to generate a response."""


class LLMQuotaError(LLMProviderError):
    """Raised when an LLM provider hits rate limits or quota exceeded errors."""
