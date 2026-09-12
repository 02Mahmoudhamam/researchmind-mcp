"""
SemanticSearch Agent — service implementation.
Role: perform semantic similarity search across papers
"""

from agents.semantic_search.interface import SemanticSearchAgentInterface
from agents.semantic_search.config import semantic_search_config
from shared.models.agent import AgentInput, AgentOutput
import time


class SemanticSearchAgent(SemanticSearchAgentInterface):
    """Concrete implementation of the semantic_search agent."""

    def __init__(self):
        self._config = semantic_search_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the semantic_search task."""
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
