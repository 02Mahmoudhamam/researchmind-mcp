"""MCP Tool: extract_citations"""

from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for extract_citations tool."""

    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="extract_citations",
    description="MCP tool for extract_citations — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch extract_citations call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
