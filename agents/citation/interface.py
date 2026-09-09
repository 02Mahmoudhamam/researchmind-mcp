"""Public interface for citation agent."""
from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class CitationAgentInterface(BaseAgent):
    """Interface definition for citation agent."""

    @property
    def name(self) -> str:
        return "citation"

    @property
    def description(self) -> str:
        return "extract and format citations from documents"

    async def run(self, input: AgentInput) -> AgentOutput:
        ...  # implemented in service

    async def health_check(self) -> bool:
        ...  # implemented in service
