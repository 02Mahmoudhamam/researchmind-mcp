"""MCP Prompt: Scientific Reviewer"""

from mcp.types import Prompt, PromptArgument, PromptMessage

schema = Prompt(
    name="scientific_reviewer_prompt",
    description="Act as a scientific peer reviewer",
    arguments=[
        PromptArgument(
            name="document_id", description="Paper to review", required=True
        ),
    ],
)


async def render(arguments: dict) -> list[PromptMessage]:
    # TODO(M6): render the template and return list[mcp.types.TextContent].
    ...
    return []
