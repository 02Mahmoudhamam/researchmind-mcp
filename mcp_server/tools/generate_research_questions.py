"""MCP Tool: generate_research_questions"""
from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for generate_research_questions tool."""
    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="generate_research_questions",
    description="MCP tool for generate_research_questions — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch generate_research_questions call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
