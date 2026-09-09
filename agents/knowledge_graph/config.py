"""Configuration for knowledge_graph agent."""
from shared.models.agent import AgentConfig

knowledge_graph_config = AgentConfig(
    name="knowledge_graph",
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    temperature=0.3,
    timeout_seconds=120,
)
