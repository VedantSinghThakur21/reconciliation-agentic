"""Agents package — optional external agent integrations."""

from src.agents.crewai_client import (
    CrewAIClient,
    CrewAIClientError,
    crewai_configured,
    enrich_leftovers_with_crewai,
)

__all__ = [
    "CrewAIClient",
    "CrewAIClientError",
    "crewai_configured",
    "enrich_leftovers_with_crewai",
]
