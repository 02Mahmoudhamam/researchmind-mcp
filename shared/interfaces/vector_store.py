"""Vector store interface."""
from abc import ABC, abstractmethod
from typing import List, Optional
from shared.models.document import DocumentChunk, SearchResult


class BaseVectorStore(ABC):
    """Interface for vector database operations."""

    @abstractmethod
    async def upsert(self, chunks: List[DocumentChunk]) -> bool: ...

    @abstractmethod
    async def search(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: float = 0.7,
        filters: Optional[dict] = None,
    ) -> List[SearchResult]: ...

    @abstractmethod
    async def delete_by_document_id(self, document_id: str) -> bool: ...

    @abstractmethod
    async def count(self) -> int: ...
