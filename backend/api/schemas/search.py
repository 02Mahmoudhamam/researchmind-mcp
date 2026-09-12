"""Search request/response schemas."""

from pydantic import BaseModel
from typing import List, Optional
from shared.models.document import SearchResult


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    score_threshold: float = 0.7
    document_ids: Optional[List[str]] = None


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]
    total: int
