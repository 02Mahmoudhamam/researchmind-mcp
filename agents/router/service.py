"""
Router Agent — service implementation.
Role: route user requests to the correct specialized agent
"""
from agents.router.interface import RouterAgentInterface
from agents.router.config import router_config
from agents.router.prompt import build_prompt
from shared.models.agent import AgentInput, AgentOutput
import time


class RouterAgent(RouterAgentInterface):
    """Concrete implementation of the router agent."""

    def __init__(self):
        self._config = router_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the router task."""
        start = time.monotonic()
        try:
            prompt = build_prompt(input.task, str(input.context))
            # TODO: call Claude API via anthropic client
            result = None
            return AgentOutput(
                agent_name=self.name,
                session_id=input.session_id,
                success=True,
                result=result,
                latency_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as e:
            return AgentOutput(
                agent_name=self.name,
                session_id=input.session_id,
                success=False,
                error=str(e),
                latency_ms=(time.monotonic() - start) * 1000,
            )

    async def health_check(self) -> bool:
        """Verify agent operational status."""
        return True
