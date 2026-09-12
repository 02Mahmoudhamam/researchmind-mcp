"""Semantic search service."""

from document_processing.embedder import EmbeddingGenerator
from vector_db.qdrant.repository import QdrantVectorRepository
from shared.models.document import SearchResult
from typing import List, Optional


class SearchService:
    def __init__(self):
        self._embedder = EmbeddingGenerator()
        self._vector_store = QdrantVectorRepository()

    async def search(
        self,
        query: str,
        limit: int = 10,
        score_threshold: float = 0.7,
        document_ids: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """Embed query and run similarity search."""
        ...  # TODO: embed → search → filter by document_ids
