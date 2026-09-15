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

The ingestion worker (M3/S3.2) has no Principal, but it has an owner: the
`owner_id` its job carries. It uses the same rule — its reads and every status
transition put that owner in the WHERE clause, so a job naming the wrong owner
cannot touch the document even if the worker's own comparison were removed.
One method is a deliberate, documented exception, and it returns no row:
`live_document_exists`, a boolean, to tell a forged job from a deleted document
in operator logs.
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
from shared.models.ingestion import IngestionTarget


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
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        page_count=row.page_count,
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
        storage_key: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        mime_type: str | None = None,
        page_count: int | None = None,
    ) -> Document:
        """Create a document owned by `user_id`.

        No pre-check that the user exists: the foreign key already refuses an
        invented owner, and a check followed by an insert is a race. A malformed
        user id raises ValueError rather than silently creating nothing, because
        here it is a programming error rather than untrusted path input.

        The storage fields (ADR-0008) are optional because documents without
        stored bytes are legitimate — every row created before M3/S3.1. Upload
        sets all five. A second live document with the same owner and content
        hash raises the IntegrityError from `uq_documents_user_id_content_hash`
        (ADR-0010); this does not pre-check for the same reason as above.
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
            storage_key=storage_key,
            content_hash=content_hash,
            size_bytes=size_bytes,
            mime_type=mime_type,
            page_count=page_count,
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

    async def find_live_by_content_hash_for_user(
        self, content_hash: str, user_id: str
    ) -> Document | None:
        """This user's live document with this content, if they have one.

        The lookup behind ADR-0010's per-owner idempotent upload. Owner and
        `deleted_at IS NULL` are in the WHERE clause like every other read here,
        so it cannot answer "does *anyone* have this file" — which would be a
        cross-tenant existence oracle — and a deleted document never counts.

        A fast path, not the rule: two concurrent uploads can both miss here,
        and the partial unique index is what stops the second insert.
        """
        owner = parse_id(user_id)
        if owner is None:
            return None

        result = await self._session.execute(
            select(DocumentORM).where(
                DocumentORM.content_hash == content_hash,
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

    # --- Ingestion (M3/S3.2) -------------------------------------------------

    async def get_ingestion_target_for_user(
        self, document_id: str, user_id: str
    ) -> IngestionTarget | None:
        """What the worker needs about this user's live document, or None.

        Owner and `deleted_at IS NULL` in the WHERE clause, as everywhere else:
        None covers no such document, a deleted one, and one that belongs to
        someone else. The worker passes the job's `owner_id`, so a job naming
        the wrong owner reads nothing.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return None

        result = await self._session.execute(
            select(
                DocumentORM.id,
                DocumentORM.user_id,
                DocumentORM.status,
                DocumentORM.storage_key,
                DocumentORM.content_hash,
                DocumentORM.mime_type,
            ).where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return IngestionTarget(
            document_id=str(row.id),
            owner_id=str(row.user_id),
            status=row.status,
            storage_key=row.storage_key,
            content_hash=row.content_hash,
            mime_type=row.mime_type,
        )

    async def live_document_exists(self, document_id: str) -> bool:
        """Whether *any* live document has this id. Returns nothing else.

        A documented exception to the owner rule, and deliberately the least
        this could return. The worker calls it only after an owner-scoped read
        came back empty, to tell an operator which of two very different things
        happened: a document that was deleted (routine) or a job that names the
        wrong owner (a forged or corrupted job, worth attention). No row, no
        owner id and no field of the document leaves this method, and nothing
        reachable from a request calls it.
        """
        target = parse_id(document_id)
        if target is None:
            return False
        result = await self._session.execute(
            select(DocumentORM.id).where(
                DocumentORM.id == target, DocumentORM.deleted_at.is_(None)
            )
        )
        return result.first() is not None

    async def transition_status_for_user(
        self,
        document_id: str,
        user_id: str,
        *,
        expected: DocumentStatus,
        new: DocumentStatus,
        failure_reason: str | None = None,
    ) -> bool:
        """Move this user's live document from `expected` to `new`. Atomic.

        One UPDATE whose WHERE carries id, owner, `deleted_at IS NULL` **and the
        expected status** — a compare-and-set. The database decides whether the
        document was still where the caller believed it was, so two workers
        racing for the same document cannot both succeed, and a worker whose
        claim was taken by the reaper cannot overwrite the reaper's verdict.
        Returns False when nothing matched, for any of those reasons.

        `failure_reason` is written as given — including None, which clears it —
        so a document can never keep the reason for a failure it has left. The
        CHECK constraint `failure_reason_iff_failed` refuses a mismatch.

        `updated_at` is set from the database clock: it is what the stale-job
        reaper measures a claim's age by.

        Flushes nothing and commits nothing beyond executing the statement; the
        service owns the transaction.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return False

        result = await self._session.execute(
            update(DocumentORM)
            .where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
                DocumentORM.status == expected,
            )
            .values(status=new, failure_reason=failure_reason, updated_at=func.now())
        )
        return bool(cast("CursorResult[Any]", result).rowcount)
