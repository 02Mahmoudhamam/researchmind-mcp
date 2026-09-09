"""Unit tests for agent services."""
import pytest
from shared.models.agent import AgentInput, AgentOutput


@pytest.fixture
def sample_input():
    return AgentInput(session_id="test-session", user_id="user-1", task="Summarize this paper")


@pytest.mark.asyncio
async def test_summarizer_agent_returns_output(sample_input):
    from agents.summarizer.service import SummarizerAgent
    agent = SummarizerAgent()
    result = await agent.run(sample_input)
    assert isinstance(result, AgentOutput)
    assert result.agent_name == "summarizer"


@pytest.mark.asyncio
async def test_agent_health_check():
    from agents.summarizer.service import SummarizerAgent
    agent = SummarizerAgent()
    assert await agent.health_check() is True
