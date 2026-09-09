"""MCP Prompt: Citation Extraction"""
from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent

schema = Prompt(
    name="citation_extraction_prompt",
    description="Extract and format all citations from a document",
    arguments=[
        PromptArgument(name="document_id", required=True, description="Document to extract from"),
        PromptArgument(name="format", required=False, description="Output format: APA|MLA|Chicago"),
    ],
)

async def render(arguments: dict) -> list[PromptMessage]:
    ...  # TODO: implement
    return []
