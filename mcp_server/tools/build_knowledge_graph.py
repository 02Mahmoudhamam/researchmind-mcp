"""MCP Tool: build_knowledge_graph"""

from mcp.types import Tool, TextContent
from pydantic import BaseModel
from typing import List, Optional


class Input(BaseModel):
    """Input schema for build_knowledge_graph tool."""

    document_ids: List[str]
    options: Optional[dict] = None


schema = Tool(
    name="build_knowledge_graph",
    description="MCP tool for build_knowledge_graph — connects to the agent layer",
    inputSchema=Input.model_json_schema(),
)


async def handler(arguments: dict) -> list[TextContent]:
    """Dispatch build_knowledge_graph call to the appropriate agent."""
    ...  # TODO: call agent service
    return [TextContent(type="text", text="Not yet implemented")]
