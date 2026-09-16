"""What retrieval takes and what it returns — M4/S4.1.

`SearchResult` in `document.py` is the scaffold's API shape: a whole
`DocumentChunk` (including an `embedding` field retrieval never populates) and
an optional whole `Document`. These are the service layer's own types, carrying
what a citation needs and nothing an infrastructure detail would leak.

Every field on `RetrievalResult` comes from **PostgreSQL**, read after
ownership and status were checked in the same statement (ADR-0014 §1). Nothing
here is copied from a Qdrant payload except the score, which is not an
authorization decision.
"""

from pydantic import BaseModel, ConfigDict, Field


class ValidatedChunk(BaseModel):
    """A chunk PostgreSQL has confirmed this principal may retrieve.

    What the database knows. It carries **no score**, because the database has
    no opinion about one — the repository that builds these has never seen a
    query vector. The service pairs each with the score the vector store
    returned; keeping the two apart means no layer has to invent a placeholder
    for a value it does not have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The chunk's id, which is also its Qdrant point id (ADR-0013 §4).
    chunk_id: str
    # Read from the database row, never from the payload that proposed it.
    document_id: str

    # The authoritative text. ADR-0013 §5 keeps it out of the vector store, so
    # this is the only place it can come from — and it arrives already
    # validated (ADR-0014 §7).
    content: str

    # ADR-0007 §1's provenance: what a citation resolves against.
    chunk_index: int
    section: str | None = None
    page_start: int
    page_end: int

    # Which model embedded it. Validation refuses any other (ADR-0014 §4), so
    # this is the active one — recorded because a result outlives its search.
    embedding_model_id: str


class RetrievalResult(BaseModel):
    """One validated chunk, and how well it matched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: str

    # Qdrant's similarity for the query vector. Comparable *within* one search
    # and against `RETRIEVAL_SCORE_THRESHOLD`; not a probability, and not
    # comparable across embedding models.
    score: float

    content: str
    chunk_index: int
    section: str | None = None
    page_start: int
    page_end: int
    embedding_model_id: str

    @classmethod
    def of(cls, chunk: ValidatedChunk, score: float) -> "RetrievalResult":
        """Pair what the database confirmed with what the index measured."""
        return cls(score=score, **chunk.model_dump())


class RetrievalQuery(BaseModel):
    """A search as the service accepts it.

    **There is no owner field, deliberately.** The owner is the authenticated
    `Principal` the caller passes separately, so no request body — and no MCP
    tool argument, and no future HTTP client — can express "search as someone
    else". It is not that a forged owner is rejected; it is that there is
    nowhere to put one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str

    # None means "the configured default". Validated by the service against
    # `RETRIEVAL_TOP_K` and `RETRIEVAL_SCORE_THRESHOLD`.
    top_k: int | None = Field(default=None, gt=0)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    # Narrows the search to these documents. It can only ever narrow: the owner
    # filter is applied alongside it, so naming another tenant's document
    # matches nothing (ADR-0014 §1).
    document_ids: tuple[str, ...] | None = None
