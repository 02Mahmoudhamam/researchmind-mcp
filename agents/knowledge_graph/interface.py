"""Public interface for knowledge_graph agent."""

from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class KnowledgeGraphAgentInterface(BaseAgent):
    """Interface definition for knowledge_graph agent."""

    @property
    def name(self) -> str:
        return "knowledge_graph"

    @property
    def description(self) -> str:
        return "build and query knowledge graphs from papers"

    async def run(self, input: AgentInput) -> AgentOutput: ...  # implemented in service

    async def health_check(self) -> bool: ...  # implemented in service
