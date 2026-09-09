"""Prompt templates for orchestrator agent."""

SYSTEM_PROMPT = """You are the Orchestrator agent in ResearchMind MCP.
Your role: orchestrate multi-agent research workflows.
Always respond in structured format unless instructed otherwise.
"""

def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
