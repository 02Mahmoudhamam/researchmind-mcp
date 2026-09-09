"""Public interface for paper_comparison agent."""
from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class PaperComparisonAgentInterface(BaseAgent):
    """Interface definition for paper_comparison agent."""

    @property
    def name(self) -> str:
        return "paper_comparison"

    @property
    def description(self) -> str:
        return "compare multiple research papers"

    async def run(self, input: AgentInput) -> AgentOutput:
        ...  # implemented in service

    async def health_check(self) -> bool:
        ...  # implemented in service
