"""Persistence model for one page of a document's extracted text — M3/S3.3.

Why a table of its own. The text has to outlive the job that extracted it — the
chunking stage reads it — and the schema had nowhere suitable to put it:

* `documents` is read by every list request. A column holding a whole
  document's text would be loaded with every row of every list.
* `document_chunks` is the chunking stage's output, with a `chunk_index`,
  embedding columns and, per ADR-0007, sections still to come. Extracted pages
  are its *input*; writing them as chunks would claim a chunking that has not
  happened.

One row per page, keyed `(document_id, page_number)`. The composite primary key
is also the guard against extracting a document twice: the same page cannot be
stored twice, whatever a duplicate delivery tries. A page with no text is still
a row, with no blocks, so page numbers are never inferred.

The blocks are JSONB — `[{"text": ..., "font_size": ...}, ...]` in reading
order — rather than a row per block: the chunking stage reads a page's blocks
together and in order, never one block alone, and a 500-page document is 500
rows, not tens of thousands.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class DocumentPageORM(Base):
    """The extracted text of one page of one document."""

    __tablename__ = "document_pages"

    # ON DELETE CASCADE, as for chunks: extracted text is derived from the
    # document and has no existence without it. Fires only on a hard delete —
    # the normal path soft-deletes the document, and owner-scoped reads join on
    # `documents.deleted_at IS NULL`.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )

    # From 1, as a reader numbers pages and as citations will.
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)

    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("jsonb_typeof(blocks) = 'array'", name="blocks_is_array"),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentPageORM document_id={self.document_id} "
            f"page={self.page_number}>"
        )
