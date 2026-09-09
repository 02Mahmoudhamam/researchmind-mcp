"""Qdrant implementation of BaseVectorStore."""
from shared.interfaces.vector_store import BaseVectorStore
from shared.models.document import DocumentChunk, SearchResult
from vector_db.qdrant.client import get_qdrant_client
from vector_db.qdrant.config import QdrantConfig
from qdrant_client import AsyncQdrantClient
from typing import List, Optional


class QdrantVectorRepository(BaseVectorStore):
    """Production Qdrant vector store implementation."""

    def __init__(self):
        self._client: AsyncQdrantClient = get_qdrant_client()
        self._config = QdrantConfig()

    async def upsert(self, chunks: List[DocumentChunk]) -> bool:
        """Insert or update document chunks with their embeddings."""
        ...  # TODO: convert to PointStruct and upsert

    async def search(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: float = 0.7,
        filters: Optional[dict] = None,
    ) -> List[SearchResult]:
        """Perform ANN similarity search."""
        ...  # TODO: build Filter and call search

    async def delete_by_document_id(self, document_id: str) -> bool:
        """Remove all vectors belonging to a document."""
        ...  # TODO: implement with must filter

    async def count(self) -> int:
        """Return total number of vectors in collection."""
        ...  # TODO: implement
        return 0
