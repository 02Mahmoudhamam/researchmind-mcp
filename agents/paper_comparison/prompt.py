"""Prompt templates for paper_comparison agent."""

SYSTEM_PROMPT = """You are the PaperComparison agent in ResearchMind MCP.
Your role: compare multiple research papers.
Always respond in structured format unless instructed otherwise.
"""


def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
