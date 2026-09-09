"""Configuration for semantic_search agent."""
from shared.models.agent import AgentConfig

semantic_search_config = AgentConfig(
    name="semantic_search",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
