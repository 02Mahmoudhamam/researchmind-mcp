"""
KnowledgeGraph Agent — service implementation.
Role: build and query knowledge graphs from papers
"""
from agents.knowledge_graph.interface import KnowledgeGraphAgentInterface
from agents.knowledge_graph.config import knowledge_graph_config
from agents.knowledge_graph.prompt import build_prompt
from shared.models.agent import AgentInput, AgentOutput
import time


class KnowledgeGraphAgent(KnowledgeGraphAgentInterface):
    """Concrete implementation of the knowledge_graph agent."""

    def __init__(self):
        self._config = knowledge_graph_config

    async def run(self, input: AgentInput) -> AgentOutput:
        """Execute the knowledge_graph task."""
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
