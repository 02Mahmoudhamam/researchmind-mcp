"""Prompt templates for memory agent."""

SYSTEM_PROMPT = """You are the Memory agent in ResearchMind MCP.
Your role: manage session memory and long-term context.
Always respond in structured format unless instructed otherwise.
"""

def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
