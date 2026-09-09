"""Base agent interface — all agents must implement this."""
from abc import ABC, abstractmethod
from typing import Any
from shared.models.agent import AgentInput, AgentOutput


class BaseAgent(ABC):
    """Abstract base class for all ResearchMind agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent identifier."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable agent description."""
        ...

    @abstractmethod
    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the agent's primary task."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify agent is operational."""
        ...
