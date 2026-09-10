"""MCP Tool: semantic_search"""
from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for semantic_search tool."""
    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="semantic_search",
    description="MCP tool for semantic_search — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch semantic_search call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
