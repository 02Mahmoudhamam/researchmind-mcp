"""Semantic search service."""

from shared.interfaces.embedding import EmbeddingProvider
from vector_db.qdrant.repository import QdrantVectorRepository
from shared.models.document import SearchResult
from typing import List, Optional


class SearchService:
    def __init__(self):
        # TODO(M4): the query must be embedded by the same provider that
        # embedded the documents (ADR-0005 §5), injected rather than built
        # here — searching with a second model compares two vector spaces.
        self._embedder: EmbeddingProvider | None = None
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
