"""MCP Prompt: Paper Comparison"""

from mcp.types import Prompt, PromptArgument, PromptMessage

schema = Prompt(
    name="paper_comparison_prompt",
    description="Compare two or more research papers side by side",
    arguments=[
        PromptArgument(
            name="document_ids", required=True, description="Papers to compare"
        ),
        PromptArgument(
            name="criteria", required=False, description="Comparison dimensions"
        ),
    ],
)


async def render(arguments: dict) -> list[PromptMessage]:
    # TODO(M6): render the template and return list[mcp.types.TextContent].
    ...
    return []
