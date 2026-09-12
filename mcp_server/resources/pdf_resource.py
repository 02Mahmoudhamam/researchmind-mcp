"""MCP Resource: pdf_resource"""

from mcp.types import Resource


schema = Resource(
    uri="research://pdf_resource",
    name="pdf_resource",
    description="MCP resource for pdf_resource",
    mimeType="application/json",
)


async def read(uri: str) -> str:
    """Read and return resource content."""
    ...  # TODO: implement
    return "{}"
