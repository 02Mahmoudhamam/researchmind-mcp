"""Data access for document chunks.

A chunk has no owner of its own. It belongs to a document, and the document
belongs to a user, so every user-facing method here reaches ownership through a
join rather than through a column on the chunk:

    SELECT ... FROM document_chunks
    JOIN documents ON document_chunks.document_id = documents.id
    WHERE document_chunks.id = :chunk_id
      AND documents.user_id    = :user_id
      AND documents.deleted_at IS NULL

Not: fetch the chunk, then load its document, then compare. That version reads
the row before deciding whether the caller may see it, and the comparison is one
`if` a refactor can drop.

There is deliberately no `get(chunk_id)`. ADR-0003 makes the re-validation path
— "every returned chunk id is re-fetched from PostgreSQL and any not owned by
the principal are dropped" — the single most safety-critical read in the system,
and it must not have an unscoped alternative sitting next to it.

Chunk *generation*, embedding and Qdrant are M3 and M4. This writes and reads
rows, nothing more.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DocumentChunkORM, DocumentORM
from backend.db.repositories._identifiers import parse_id
from shared.models.document import DocumentChunk


def _to_domain(row: DocumentChunkORM) -> DocumentChunk:
    """Map the ORM row to the API contract.

    `embedding` is always None: the vector lives in Qdrant and PostgreSQL does
    not duplicate it. The persistence-only columns `embedding_model_id` and
    `dimension` have no field on the domain model, so they are not carried —
    M4 reads them directly when it needs to decide what to re-index.
    """
    return DocumentChunk(
        id=str(row.id),
        document_id=str(row.document_id),
        content=row.content,
        embedding=None,
        chunk_index=row.chunk_index,
        metadata=dict(row.chunk_metadata or {}),
    )


class DocumentChunkRepository:
    """Chunks, always reached through the owning document."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _owned_document_id(self, document_id: str, user_id: str) -> object | None:
        """Resolve a document id only if this user owns it and it is live.

        One query, used by every method below, so the ownership predicate is
        written once and cannot be forgotten in one place but not another.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return None

        result = await self._session.execute(
            select(DocumentORM.id).where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def add_many(
        self,
        *,
        document_id: str,
        user_id: str,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        """Append chunks to a document this user owns.

        Raises LookupError if the document does not exist, is not this user's,
        or is soft-deleted. Reads in this module return None or an empty list
        for those cases; a write does not, and the asymmetry is deliberate — a
        read finding nothing is ordinary, while a write that cannot be
        attributed to an owned document is a bug, and returning an empty list
        would be another instance of the "reported success, did nothing"
        failure this project keeps finding.

        LookupError, not a bespoke exception type: the stdlib already means
        exactly this, and a new error hierarchy is not this sprint's to invent.
        """
        owned = await self._owned_document_id(document_id, user_id)
        if owned is None:
            raise LookupError(f"no document {document_id!r} owned by user {user_id!r}")

        rows = [
            DocumentChunkORM(
                document_id=owned,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                chunk_metadata=dict(chunk.metadata),
            )
            for chunk in chunks
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return [_to_domain(row) for row in rows]

    async def get_for_user(self, chunk_id: str, user_id: str) -> DocumentChunk | None:
        """Return the chunk only if this user owns the document it belongs to.

        The join carries the ownership predicate, so a chunk belonging to
        someone else is not fetched and then rejected — it is not selected.
        """
        target, owner = parse_id(chunk_id), parse_id(user_id)
        if target is None or owner is None:
            return None

        result = await self._session.execute(
            select(DocumentChunkORM)
            .join(DocumentORM, DocumentChunkORM.document_id == DocumentORM.id)
            .where(
                DocumentChunkORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def list_for_document(
        self, document_id: str, user_id: str
    ) -> list[DocumentChunk]:
        """Return a document's chunks in order, if this user owns it.

        Empty for a document that does not exist, is not theirs, or is deleted —
        the same three cases collapsed, as everywhere else here.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return []

        result = await self._session.execute(
            select(DocumentChunkORM)
            .join(DocumentORM, DocumentChunkORM.document_id == DocumentORM.id)
            .where(
                DocumentChunkORM.document_id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .order_by(DocumentChunkORM.chunk_index)
        )
        return [_to_domain(row) for row in result.scalars().all()]
