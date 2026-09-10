"""MCP Prompt: Scientific Reviewer"""
from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent

schema = Prompt(
    name="scientific_reviewer_prompt",
    description="Act as a scientific peer reviewer",
    arguments=[
        PromptArgument(name="document_id", description="Paper to review", required=True),
    ],
)

async def render(arguments: dict) -> list[PromptMessage]:
    ...  # TODO: implement
    return []
