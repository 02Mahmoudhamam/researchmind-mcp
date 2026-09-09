"""Configuration for research_gap agent."""
from shared.models.agent import AgentConfig

research_gap_config = AgentConfig(
    name="research_gap",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
