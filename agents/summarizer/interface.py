"""Public interface for summarizer agent."""
from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class SummarizerAgentInterface(BaseAgent):
    """Interface definition for summarizer agent."""

    @property
    def name(self) -> str:
        return "summarizer"

    @property
    def description(self) -> str:
        return "summarize research papers and documents"

    async def run(self, input: AgentInput) -> AgentOutput:
        ...  # implemented in service

    async def health_check(self) -> bool:
        ...  # implemented in service
