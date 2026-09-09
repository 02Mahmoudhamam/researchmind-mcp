"""Prompt templates for router agent."""

SYSTEM_PROMPT = """You are the Router agent in ResearchMind MCP.
Your role: route user requests to the correct specialized agent.
Always respond in structured format unless instructed otherwise.
"""

def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
