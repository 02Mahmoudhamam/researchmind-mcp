"""Public interface for memory agent."""

from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class MemoryAgentInterface(BaseAgent):
    """Interface definition for memory agent."""

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return "manage session memory and long-term context"

    async def run(self, input: AgentInput) -> AgentOutput: ...  # implemented in service

    async def health_check(self) -> bool: ...  # implemented in service
