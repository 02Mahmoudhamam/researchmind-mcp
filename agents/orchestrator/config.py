"""Configuration for orchestrator agent."""

from shared.models.agent import AgentConfig

orchestrator_config = AgentConfig(
    name="orchestrator",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
