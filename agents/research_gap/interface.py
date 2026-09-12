"""Public interface for research_gap agent."""

from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class ResearchGapAgentInterface(BaseAgent):
    """Interface definition for research_gap agent."""

    @property
    def name(self) -> str:
        return "research_gap"

    @property
    def description(self) -> str:
        return "detect research gaps across document corpora"

    async def run(self, input: AgentInput) -> AgentOutput: ...  # implemented in service

    async def health_check(self) -> bool: ...  # implemented in service
