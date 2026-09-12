"""Embedding generation for document chunks."""

from typing import List
from shared.models.document import DocumentChunk


class EmbeddingGenerator:
    """Generates vector embeddings for text chunks."""

    def __init__(self, model: str = "text-embedding-3-small"):
        self.model = model
        self._client = None  # TODO: init anthropic/openai client

    async def embed_chunks(self, chunks: List[DocumentChunk]) -> List[DocumentChunk]:
        """Attach embeddings to each chunk in-place."""
        ...  # TODO: batch embed and attach

    async def embed_query(self, query: str) -> List[float]:
        """Embed a single search query string."""
        ...  # TODO: implement single embed
