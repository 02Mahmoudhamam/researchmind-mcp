"""
Summarizer Agent — service implementation.
Role: summarize research papers and documents
"""

from agents.summarizer.interface import SummarizerAgentInterface
from agents.summarizer.config import summarizer_config
from shared.models.agent import AgentInput, AgentOutput
import time


class SummarizerAgent(SummarizerAgentInterface):
    """Concrete implementation of the summarizer agent."""

    def __init__(self):
        self._config = summarizer_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the summarizer task."""
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
