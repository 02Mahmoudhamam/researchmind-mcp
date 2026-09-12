"""MCP Tool: detect_research_gaps"""

from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for detect_research_gaps tool."""

    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="detect_research_gaps",
    description="MCP tool for detect_research_gaps — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch detect_research_gaps call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
