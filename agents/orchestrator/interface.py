"""Public interface for orchestrator agent."""
from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class OrchestratorAgentInterface(BaseAgent):
    """Interface definition for orchestrator agent."""

    @property
    def name(self) -> str:
        return "orchestrator"

    @property
    def description(self) -> str:
        return "orchestrate multi-agent research workflows"

    async def run(self, input: AgentInput) -> AgentOutput:
        ...  # implemented in service

    async def health_check(self) -> bool:
        ...  # implemented in service
