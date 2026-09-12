"""
Citation Agent — service implementation.
Role: extract and format citations from documents
"""

from agents.citation.interface import CitationAgentInterface
from agents.citation.config import citation_config
from shared.models.agent import AgentInput, AgentOutput
import time


class CitationAgent(CitationAgentInterface):
    """Concrete implementation of the citation agent."""

    def __init__(self):
        self._config = citation_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the citation task."""
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
