"""Public interface for router agent."""

from shared.interfaces.agent import BaseAgent
from shared.models.agent import AgentInput, AgentOutput


class RouterAgentInterface(BaseAgent):
    """Interface definition for router agent."""

    @property
    def name(self) -> str:
        return "router"

    @property
    def description(self) -> str:
        return "route user requests to the correct specialized agent"

    async def run(self, input: AgentInput) -> AgentOutput: ...  # implemented in service

    async def health_check(self) -> bool: ...  # implemented in service
