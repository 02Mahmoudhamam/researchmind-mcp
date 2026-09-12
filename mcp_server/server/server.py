"""ResearchMind MCP Server — registers all tools, resources, and prompts."""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, Resource, Prompt
from mcp_server.tools import (
    summarize_paper_tool,
    extract_citations_tool,
    compare_papers_tool,
    semantic_search_tool,
    build_knowledge_graph_tool,
    detect_research_gaps_tool,
    generate_research_questions_tool,
)

app = Server("researchmind-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return all registered MCP tools."""
    return [
        summarize_paper_tool.schema,
        extract_citations_tool.schema,
        compare_papers_tool.schema,
        semantic_search_tool.schema,
        build_knowledge_graph_tool.schema,
        detect_research_gaps_tool.schema,
        generate_research_questions_tool.schema,
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    """Dispatch tool calls to the correct handler."""
    ...  # TODO: implement dispatch


@app.list_resources()
async def list_resources() -> list[Resource]:
    """Return all registered MCP resources."""
    # TODO(M6): mirror list_tools() above, returning the schema of each module in
    # mcp_server.resources: pdf_resource, paper_resource, notes_resource,
    # metadata_resource, knowledge_graph_resource.
    ...


@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read a specific MCP resource by URI."""
    ...  # TODO: implement


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """Return all registered MCP prompts."""
    # TODO(M6): mirror list_tools() above, returning the schema of each module in
    # mcp_server.prompts: summarization_prompt, scientific_reviewer_prompt,
    # citation_extraction_prompt, research_gap_prompt, paper_comparison_prompt,
    # question_generation_prompt.
    ...


@app.get_prompt()
async def get_prompt(name: str, arguments: dict) -> str:
    """Render a prompt template with given arguments."""
    ...  # TODO: implement


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
