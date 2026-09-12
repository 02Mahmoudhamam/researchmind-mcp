"""Prompt templates for semantic_search agent."""

SYSTEM_PROMPT = """You are the SemanticSearch agent in ResearchMind MCP.
Your role: perform semantic similarity search across papers.
Always respond in structured format unless instructed otherwise.
"""


def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
