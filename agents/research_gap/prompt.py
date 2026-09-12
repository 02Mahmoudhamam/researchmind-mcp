"""Prompt templates for research_gap agent."""

SYSTEM_PROMPT = """You are the ResearchGap agent in ResearchMind MCP.
Your role: detect research gaps across document corpora.
Always respond in structured format unless instructed otherwise.
"""


def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
