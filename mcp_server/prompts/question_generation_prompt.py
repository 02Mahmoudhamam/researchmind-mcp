"""MCP Prompt: Research Question Generation"""

from mcp.types import Prompt, PromptArgument, PromptMessage

schema = Prompt(
    name="question_generation_prompt",
    description="Generate insightful research questions from papers",
    arguments=[
        PromptArgument(
            name="document_id", required=True, description="Source document"
        ),
        PromptArgument(
            name="count", required=False, description="Number of questions to generate"
        ),
    ],
)


async def render(arguments: dict) -> list[PromptMessage]:
    # TODO(M6): render the template and return list[mcp.types.TextContent].
    ...
    return []
