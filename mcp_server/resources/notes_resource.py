"""MCP Resource: notes_resource"""
from mcp.types import Resource


schema = Resource(
    uri="research://notes_resource",
    name="notes_resource",
    description="MCP resource for notes_resource",
    mimeType="application/json",
)


async def read(uri: str) -> str:
    """Read and return resource content."""
    ...  # TODO: implement
    return "{}"
