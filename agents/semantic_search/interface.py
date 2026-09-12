"""Public interface for semantic_search agent."""

from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class SemanticSearchAgentInterface(BaseAgent):
    """Interface definition for semantic_search agent."""

    @property
    def name(self) -> str:
        return "semantic_search"

    @property
    def description(self) -> str:
        return "perform semantic similarity search across papers"

    async def run(self, input: AgentInput) -> AgentOutput: ...  # implemented in service

    async def health_check(self) -> bool: ...  # implemented in service
