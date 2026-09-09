"""Prompt templates for citation agent."""

SYSTEM_PROMPT = """You are the Citation agent in ResearchMind MCP.
Your role: extract and format citations from documents.
Always respond in structured format unless instructed otherwise.
"""

def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
