"""MCP Tool: summarize_paper"""

from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for summarize_paper tool."""

    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="summarize_paper",
    description="MCP tool for summarize_paper — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch summarize_paper call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
