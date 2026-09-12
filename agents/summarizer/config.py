"""Configuration for summarizer agent."""

from shared.models.agent import AgentConfig

summarizer_config = AgentConfig(
    name="summarizer",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
