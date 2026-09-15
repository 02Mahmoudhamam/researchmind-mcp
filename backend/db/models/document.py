"""Persistence model for a document.

The record of what exists and who owns it, and — since M3/S3.1 — where its bytes
are and what they are (ADR-0008). Parsing adds its own columns when it lands.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from shared.models.document import DocumentStatus, DocumentType

if TYPE_CHECKING:
    from backend.db.models.document_chunk import DocumentChunkORM
    from backend.db.models.user import UserORM


class DocumentORM(Base):
    """A document owned by exactly one user."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # ON DELETE RESTRICT, not CASCADE. Deleting a user would otherwise destroy
    # their entire corpus as a side effect, and nothing in the system asks for
    # that: deactivation is `users.is_active`, and no user-delete path exists.
    # RESTRICT makes the destructive version impossible at the database level
    # rather than merely absent from the code.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # Display metadata only. ADR-0008 is explicit that the client-supplied
    # filename never forms part of a filesystem path, which is what makes path
    # traversal structurally impossible rather than defended against.
    filename: Mapped[str] = mapped_column(String(512), nullable=False)

    doc_type: Mapped[DocumentType] = mapped_column(
        Enum(
            DocumentType,
            name="document_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=DocumentStatus.PENDING,
    )

    # `metadata` is reserved on a declarative class — it is Base.metadata. The
    # column is therefore mapped under a different attribute name; the Pydantic
    # field stays `metadata`, so the API contract is unchanged.
    #
    # JSONB rather than JSON: it is queryable and indexable, and the bibliographic
    # fields in DocumentMetadata (doi, year, authors) are the obvious things to
    # filter on later. Structuring them into columns would be speculative now.
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )

    # Denormalised counter, already part of the domain model and the API
    # contract. M3 maintains it in the same transaction as the chunk insert;
    # document_chunks exists, so it can always be reconciled.
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Stored content (ADR-0008), written by upload in M3/S3.1 ------------
    #
    # All five nullable, for the reason `users.password_hash` is: rows that
    # predate upload exist — every document M1 and M2 created — and they have
    # no bytes anywhere. Making these NOT NULL would mean inventing a storage
    # key and a hash for a file that was never stored. Upload always sets all
    # five; the upload tests assert it.

    # `{user_id}/{sha256}.pdf`, relative to the storage root. Never an absolute
    # path and never derived from `filename`. 105 characters today; 255 leaves
    # room for a longer extension or a prefixed backend without a migration.
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # SHA-256 of the content, lowercase hex. Part of the key, and the column the
    # per-owner uniqueness rule below is defined on (ADR-0010).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # BIGINT: the configured cap is 50 MiB, but a cap is configuration and a
    # column type is a migration.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Sniffed from the bytes, never taken from the client's Content-Type.
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Counted at upload, because the page cap in principles.md §4 has to be
    # enforced before the file is stored.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Why ingestion failed — ADR-0009 §4, "with the failure reason persisted".
    # A stable code from `IngestionReason`, never an exception message: this
    # column must be safe to show an operator and must not carry a path. Set if
    # and only if `status` is `failed`, which a CHECK constraint enforces.
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Soft delete, required by ADR-0003 and security/principles.md: the fixed
    # deletion order is "soft-delete in PostgreSQL (committed first) -> delete
    # vectors in Qdrant -> finalise". That sequence is unexpressible without a
    # column to mark the row with. The enforcement — filtering every read on
    # `deleted_at IS NULL` — is S1.3's.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped["UserORM"] = relationship(back_populates="documents")

    chunks: Mapped[list["DocumentChunkORM"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        # Let PostgreSQL do the cascading. Without this SQLAlchemy loads every
        # chunk row into memory to delete them one by one, which for a large
        # document is thousands of statements to achieve what the foreign key
        # already guarantees.
        passive_deletes=True,
    )

    __table_args__ = (
        # The only list query the application makes: DocumentService
        # .list_user_documents, newest first, excluding deleted rows. Partial,
        # because rows that are soft-deleted are never listed and would
        # otherwise grow the index for nothing.
        #
        # (user_id, created_at) without an explicit DESC: PostgreSQL scans a
        # btree backwards just as efficiently, so the direction buys nothing
        # here and costs a non-obvious index definition.
        Index(
            None,
            "user_id",
            "created_at",
            postgresql_where=deleted_at.is_(None),
        ),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        # NULL passes a CHECK, so rows without stored content are unaffected.
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("page_count > 0", name="page_count_positive"),
        # A failed document always says why, and no other document carries a
        # reason — so a stale reason cannot survive a status change, and a
        # failure can never be recorded without one.
        CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="failure_reason_iff_failed",
        ),
        # ADR-0010: one live document per owner per content. The constraint,
        # not the service's lookup, is what makes this true — two concurrent
        # uploads of the same file both pass a lookup. Partial on
        # `deleted_at IS NULL`, so deleting a document and uploading the file
        # again is allowed; and NULL hashes never collide, so pre-upload rows
        # are untouched.
        Index(
            "uq_documents_user_id_content_hash",
            "user_id",
            "content_hash",
            unique=True,
            postgresql_where=deleted_at.is_(None),
        ),
    )

    def __repr__(self) -> str:
        return f"<DocumentORM id={self.id} filename={self.filename!r}>"
