"""MCP Prompt: Research Summarization"""

from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent

schema = Prompt(
    name="summarization_prompt",
    description="Generate a structured research paper summary",
    arguments=[
        PromptArgument(
            name="document_id", description="Target document ID", required=True
        ),
        PromptArgument(
            name="depth", description="Summary depth: brief|detailed", required=False
        ),
    ],
)


async def render(arguments: dict) -> list[PromptMessage]:
    doc_id = arguments.get("document_id", "")
    depth = arguments.get("depth", "detailed")
    return [
        PromptMessage(
            role="user",
            content=TextContent(
                type="text",
                text=f"Summarize document {doc_id} with {depth} depth. Include: objective, methodology, findings, limitations.",
            ),
        )
    ]
