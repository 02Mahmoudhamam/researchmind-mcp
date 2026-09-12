"""Prompt templates for summarizer agent."""

SYSTEM_PROMPT = """You are the Summarizer agent in ResearchMind MCP.
Your role: summarize research papers and documents.
Always respond in structured format unless instructed otherwise.
"""


def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
