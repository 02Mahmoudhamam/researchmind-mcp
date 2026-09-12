"""
PaperComparison Agent — service implementation.
Role: compare multiple research papers
"""

from agents.paper_comparison.interface import PaperComparisonAgentInterface
from agents.paper_comparison.config import paper_comparison_config
from shared.models.agent import AgentInput, AgentOutput
import time


class PaperComparisonAgent(PaperComparisonAgentInterface):
    """Concrete implementation of the paper_comparison agent."""

    def __init__(self):
        self._config = paper_comparison_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the paper_comparison task."""
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
