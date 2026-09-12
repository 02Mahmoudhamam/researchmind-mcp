"""
ResearchGap Agent — service implementation.
Role: detect research gaps across document corpora
"""

from agents.research_gap.interface import ResearchGapAgentInterface
from agents.research_gap.config import research_gap_config
from shared.models.agent import AgentInput, AgentOutput
import time


class ResearchGapAgent(ResearchGapAgentInterface):
    """Concrete implementation of the research_gap agent."""

    def __init__(self):
        self._config = research_gap_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the research_gap task."""
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
