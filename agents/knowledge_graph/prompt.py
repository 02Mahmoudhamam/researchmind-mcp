"""Prompt templates for knowledge_graph agent."""

SYSTEM_PROMPT = """You are the KnowledgeGraph agent in ResearchMind MCP.
Your role: build and query knowledge graphs from papers.
Always respond in structured format unless instructed otherwise.
"""

def build_prompt(task: str, context: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\nTask: {task}\n\nContext:\n{context}"
