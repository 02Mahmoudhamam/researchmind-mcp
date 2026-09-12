"""MCP Tool: compare_papers"""

from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for compare_papers tool."""

    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="compare_papers",
    description="MCP tool for compare_papers — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch compare_papers call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
