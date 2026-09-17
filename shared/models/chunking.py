"""Chunks, as the chunker produces them — M3/S3.4.

`DocumentChunk` in `document.py` is what a *read* returns: the API contract,
carrying ADR-0007's citation provenance. This is the other side — what the
chunker hands the repository, with the persistence-only facts a read has no use
for: the token count, and the strategy and tokenizer that produced it
(ADR-0012 §5).

Frozen, and ordered by `chunk_index`: the same pages, configuration, tokenizer
and strategy must produce the same chunks, in the same order, every time.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Chunk(BaseModel):
    """One chunk of one document, before it is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Position in the document, from 0 and contiguous. Ordering is meaning
    # here: neighbouring chunks are what a parent-section lookup walks.
    chunk_index: int = Field(ge=0)

    # A verbatim substring of the page text S3.3 stored — never re-joined or
    # re-normalised, so a citation can be found in the document it came from.
    content: str

    # The heading this chunk falls under, as it appears. None when no heading
    # was detected above it — the front matter of a paper, most often.
    section: str | None = None

    # Inclusive, 1-based, and never inverted.
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    # Counted with the tokenizer named in `ChunkingResult`.
    token_count: int = Field(gt=0)

    @field_validator("content")
    @classmethod
    def _has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a chunk must contain text")
        return value

    @model_validator(mode="after")
    def _pages_run_forwards(self) -> "Chunk":
        if self.page_end < self.page_start:
            raise ValueError("page_end must not precede page_start")
        return self


class ChunkingResult(BaseModel):
    """Every chunk of one document, with what produced them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunks: tuple[Chunk, ...]
    # ADR-005 §7: a strategy change must be detectable. ADR-0012 records the
    # tokenizer with it, because the boundaries depend on it and M4 replaces it.
    strategy_version: str
    tokenizer_id: str

    # Deliberately no validation that the indexes run 0..n-1 here. The chunker
    # produces them that way and its own tests say so; the database refuses
    # duplicates through `uq_document_chunks_document_id_chunk_index`. A model
    # that refused them first would stop the repository tests proving that.


# The namespace for derived chunk ids. A fixed, arbitrary UUID — it never
# changes, because changing it would change every id derived from it.
CHUNK_ID_NAMESPACE = uuid.UUID("6f1a2d4e-9b3c-5a7d-8e0f-1c2b3a4d5e6f")


def chunk_id_for(
    *,
    document_id: str,
    chunk_index: int,
    strategy_version: str,
    tokenizer_id: str,
) -> uuid.UUID:
    """The id of one chunk, derived rather than drawn (ADR-0013 §4).

    A chunk's id is also its Qdrant point id, so deriving it is what makes the
    whole pipeline idempotent: re-running the embed stage after a crash, a
    duplicate delivery or a partial upsert writes the *same* points and
    overwrites them, instead of leaving a second vector for every chunk.

    The four inputs are exactly what determines a chunk's content. The document
    and the index place it; the strategy and tokenizer decide where its
    boundaries fall, so a change to either must produce different ids — which is
    why re-chunking deletes the old vectors rather than leaving them to collide
    with ids that no longer match.

    Deliberately *not* derived from the content: two identical chunks in one
    document would collide, and a re-chunk that changed nothing but whitespace
    would churn every id after it.
    """
    return uuid.uuid5(
        CHUNK_ID_NAMESPACE,
        f"{document_id}|{chunk_index}|{strategy_version}|{tokenizer_id}",
    )
