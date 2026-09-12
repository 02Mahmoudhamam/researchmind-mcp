"""MCP Tool definitions and handlers."""

from mcp_server.tools import (
    summarize_paper as summarize_paper_tool,
    extract_citations as extract_citations_tool,
    compare_papers as compare_papers_tool,
    semantic_search as semantic_search_tool,
    build_knowledge_graph as build_knowledge_graph_tool,
    detect_research_gaps as detect_research_gaps_tool,
    generate_research_questions as generate_research_questions_tool,
)

# These aliases are the package's public surface: mcp_server.server.server
# imports them from here and reads `.schema` off each. They are re-exports, not
# dead imports, so they are declared rather than removed.
__all__ = [
    "summarize_paper_tool",
    "extract_citations_tool",
    "compare_papers_tool",
    "semantic_search_tool",
    "build_knowledge_graph_tool",
    "detect_research_gaps_tool",
    "generate_research_questions_tool",
]
