"""Agent orchestration service — routes tasks to the right agent."""

from shared.models.agent import AgentInput, AgentOutput
from agents.orchestrator.service import OrchestratorAgent
from shared.utils.id_generator import generate_id


class AgentService:
    def __init__(self):
        self._orchestrator = OrchestratorAgent()

    async def run(
        self, task: str, document_ids: list, user_id: str, session_id: str | None
    ) -> AgentOutput:
        """Route a task through the orchestrator agent."""
        session_id = session_id or generate_id()
        input = AgentInput(
            session_id=session_id,
            user_id=user_id,
            task=task,
            payload={"document_ids": document_ids},
        )
        return await self._orchestrator.run(input)
