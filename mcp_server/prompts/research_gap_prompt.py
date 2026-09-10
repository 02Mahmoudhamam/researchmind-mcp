"""MCP Prompt: Research Gap Analysis"""
from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent

schema = Prompt(
    name="research_gap_prompt",
    description="Identify research gaps across a set of papers",
    arguments=[
        PromptArgument(name="document_ids", required=True, description="Comma-separated document IDs"),
    ],
)

async def render(arguments: dict) -> list[PromptMessage]:
    ...  # TODO: implement
    return []
