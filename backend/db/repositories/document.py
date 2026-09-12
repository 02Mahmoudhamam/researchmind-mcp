"""Data access for documents.

Every method that can reach a user-owned row takes `user_id` as a **required**
parameter, and that parameter goes into the SQL predicate — not into an `if`
after the row comes back.

The distinction matters more than it looks. Fetching by id and then comparing
`row.user_id` in Python is a check a future refactor can delete without any
test noticing, and until it is deleted the row has already been loaded into the
process. Putting ownership in the WHERE clause means the unsafe version is not
a missing `if`, it is a different query — and the tests in
tests/integration/test_repository_security.py assert the generated SQL still
contains both predicates.

There is deliberately no `get_by_id(document_id)`. A caller who wants one has to
say whose it is.
"""

from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DocumentORM
from backend.db.repositories._identifiers import parse_id
from shared.models.document import (
    Document,
    DocumentMetadata,
    DocumentStatus,
    DocumentType,
)


def _to_domain(row: DocumentORM) -> Document:
    """Map the ORM row to the API contract."""
    return Document(
        id=str(row.id),
        user_id=str(row.user_id),
        filename=row.filename,
        doc_type=row.doc_type,
        status=row.status,
        # The column is JSONB; DocumentMetadata ignores unknown keys, so a
        # payload written by a newer version does not break an older reader.
        metadata=DocumentMetadata(**(row.doc_metadata or {})),
        chunk_count=row.chunk_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class DocumentRepository:
    """Documents, always within an ownership boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: str,
        filename: str,
        doc_type: DocumentType,
        metadata: DocumentMetadata | None = None,
        status: DocumentStatus = DocumentStatus.PENDING,
    ) -> Document:
        """Create a document owned by `user_id`.

        No pre-check that the user exists: the foreign key already refuses an
        invented owner, and a check followed by an insert is a race. A malformed
        user id raises ValueError rather than silently creating nothing, because
        here it is a programming error rather than untrusted path input.
        """
        owner = parse_id(user_id)
        if owner is None:
            raise ValueError(f"user_id is not a valid identifier: {user_id!r}")

        row = DocumentORM(
            user_id=owner,
            filename=filename,
            doc_type=doc_type,
            status=status,
            doc_metadata=(metadata or DocumentMetadata()).model_dump(mode="json"),
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_domain(row)

    async def get_for_user(self, document_id: str, user_id: str) -> Document | None:
        """Return the document only if this user owns it and it is not deleted.

        Returns None for all three of: no such document, someone else's
        document, and a soft-deleted one. That is intentional — a caller that
        could distinguish them would be an existence oracle for other people's
        corpora.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return None

        result = await self._session.execute(
            select(DocumentORM).where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def list_for_user(self, user_id: str) -> list[Document]:
        """Return this user's documents, newest first, excluding deleted ones.

        Ordering and predicate match `ix_documents_user_id_created_at`, the
        partial index created for exactly this query.
        """
        owner = parse_id(user_id)
        if owner is None:
            return []

        result = await self._session.execute(
            select(DocumentORM)
            .where(
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .order_by(DocumentORM.created_at.desc())
        )
        return [_to_domain(row) for row in result.scalars().all()]

    async def soft_delete_for_user(self, document_id: str, user_id: str) -> bool:
        """Mark the document deleted. Returns whether anything was deleted.

        A single UPDATE whose WHERE carries id, owner and `deleted_at IS NULL`.
        Written that way rather than load-check-mutate for two reasons: the
        ownership predicate cannot be dropped without changing the query, and
        the database decides whether a row matched, so there is no window
        between the check and the write.

        Idempotent by consequence: deleting an already-deleted document matches
        zero rows and returns False, the same as a document that never existed
        or belongs to someone else. Callers cannot tell those apart, which is
        the point.

        Hard deletion is not offered. ADR-0003 fixes the order as soft-delete in
        PostgreSQL, committed first, then delete vectors in Qdrant; the Qdrant
        half lands in M4. Removing the row now would orphan vectors with nothing
        left to reconcile them against.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return False

        result = await self._session.execute(
            update(DocumentORM).where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            # func.now() so the stamp is the database server's clock, not this
            # process's — two API replicas must not disagree about when a
            # document was deleted.
            .values(deleted_at=func.now())
        )
        # session.execute is typed as Result, but DML returns a CursorResult and
        # rowcount is how the database reports whether the predicate matched —
        # which is the whole answer here.
        return bool(cast("CursorResult[Any]", result).rowcount)
