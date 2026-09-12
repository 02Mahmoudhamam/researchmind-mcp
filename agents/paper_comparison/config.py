"""Configuration for paper_comparison agent."""

from shared.models.agent import AgentConfig

paper_comparison_config = AgentConfig(
    name="paper_comparison",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
