"""Persistence model for a document chunk.

ADR-0003 is explicit that this table is not redundant with Qdrant: it is what
makes reconciliation, provenance and targeted re-indexing possible. Qdrant is
an index and can be rebuilt from here; the reverse is not true.

Chunk *generation* — parsing, section detection, chunking — is Milestone M3.
This sprint creates the table and nothing writes to it yet.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.db.models.document import DocumentORM


class DocumentChunkORM(Base):
    """A contiguous span of one document's text."""

    __tablename__ = "document_chunks"

    # This id is also the Qdrant point id. Making them the same value is what
    # turns ADR-0003's ownership re-validation — "every returned chunk id is
    # re-fetched from PostgreSQL before any content reaches the LLM" — into a
    # primary-key lookup rather than a join on a payload field.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # ON DELETE CASCADE, unlike documents -> users. Chunks are derived data with
    # no independent existence: a chunk of a document that no longer exists is
    # not a record worth keeping, it is an orphan. Note this fires only on a
    # genuine hard delete — the normal path soft-deletes the document instead.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    # Position within the document. Ordering is meaning here, not presentation:
    # neighbouring chunks are what a later parent-section lookup walks.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # `metadata` is reserved on a declarative class; see DocumentORM.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )

    # ADR-0005 puts these on the chunk rather than the document, so that a model
    # change is detectable and re-indexing can be driven from data. Per-chunk
    # granularity is the point: a partially re-embedded document is exactly the
    # state that needs finding, and a document-level column cannot express it.
    #
    # Nullable because a chunk exists from the moment it is written and is
    # embedded afterwards (M4). The vector itself is never stored here — Qdrant
    # holds it, and no ADR asks PostgreSQL to duplicate an index.
    embedding_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped["DocumentORM"] = relationship(back_populates="chunks")

    __table_args__ = (
        # Makes re-ingestion idempotent and a gap in the sequence detectable.
        # Also serves the parent lookup: document_id is the leading column, so
        # no separate index on it is needed — PostgreSQL does not index foreign
        # keys automatically, and this covers it.
        UniqueConstraint("document_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        # A dimension of zero or less is not a smaller embedding, it is a bug.
        CheckConstraint(
            "dimension IS NULL OR dimension > 0", name="dimension_positive"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentChunkORM id={self.id} "
            f"document_id={self.document_id} index={self.chunk_index}>"
        )
