"""
Memory Agent — service implementation.
Role: manage session memory and long-term context
"""

from agents.memory.interface import MemoryAgentInterface
from agents.memory.config import memory_config
from shared.models.agent import AgentInput, AgentOutput
import time


class MemoryAgent(MemoryAgentInterface):
    """Concrete implementation of the memory agent."""

    def __init__(self):
        self._config = memory_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the memory task."""
        start = time.monotonic()
        try:
            # TODO(M5): call Claude via the anthropic client with
            # build_prompt(input.task, str(input.context)).
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
