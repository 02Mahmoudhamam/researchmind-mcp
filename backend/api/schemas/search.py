"""The public shape of a search — M4/S4.2, ADR-0014 §13.

`RetrievalResult` is the **service** contract and is deliberately not
serialised to the wire. These are the HTTP types, and the separation is what
lets retrieval carry more than a client should see without a caller ever
receiving it.

The scaffold's version wrapped `shared.models.document.SearchResult`, whose
`DocumentChunk` carries an `embedding` field. Returning it would have put
vectors on the wire — an architecture test now forbids that shape.

**There is no `user_id` or `owner_id` on the request.** Searching as someone
else is not a request that gets rejected; it is one that cannot be expressed
(`principles.md` §3). The authenticated `Principal` is the only owner.
"""

from pydantic import BaseModel, ConfigDict, Field

from shared.models.retrieval import RetrievalResult


class SearchRequest(BaseModel):
    """A search as a client sends it."""

    model_config = ConfigDict(extra="forbid")

    query: str

    # Both optional: omitted means the server's configured default
    # (`RETRIEVAL_TOP_K`, `RETRIEVAL_SCORE_THRESHOLD`). The bounds match
    # `RetrievalQuery`'s, so a value the service would refuse is refused at the
    # edge with a 422 instead of reaching it.
    top_k: int | None = Field(default=None, gt=0)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    # Narrows the search to these documents. It can only narrow: the owner
    # filter is applied alongside it, so naming another tenant's document
    # matches nothing (ADR-0014 §1).
    document_ids: list[str] | None = None


class SearchHit(BaseModel):
    """One matched chunk, as a client receives it.

    Every field is read from PostgreSQL after ownership and status were checked
    (ADR-0014 §1). No vector, no ORM row, no Qdrant point, no `Principal`.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    score: float
    content: str
    chunk_index: int
    section: str | None = None
    page_start: int
    page_end: int
    # Which model produced the vector that matched. A caller comparing results
    # across a model change needs to know they are not comparable.
    embedding_model_id: str

    @classmethod
    def of(cls, result: RetrievalResult) -> "SearchHit":
        """Narrow a service result to what a client may see.

        Field by field rather than `**result.model_dump()`, so a field added to
        `RetrievalResult` later does not silently appear on the wire.
        """
        return cls(
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            score=result.score,
            content=result.content,
            chunk_index=result.chunk_index,
            section=result.section,
            page_start=result.page_start,
            page_end=result.page_end,
            embedding_model_id=result.embedding_model_id,
        )


class SearchResponse(BaseModel):
    """Results, best first.

    The query is **not** echoed back. The caller already has it, and a research
    question in a response body is a sensitive string in every proxy and access
    log that records bodies (`principles.md` §7). The scaffold's shape had a
    `query` field; nothing consumed it.
    """

    model_config = ConfigDict(extra="forbid")

    results: list[SearchHit]
    # What was returned, not what was asked for: candidates that failed
    # PostgreSQL validation are already gone (ADR-0014 §5).
    total: int
