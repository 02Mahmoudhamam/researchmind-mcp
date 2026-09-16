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

Chunk *generation* is M3/S3.4's chunker; M3/S3.5 added the reads and writes
the embed stage needs — which provenance a document's chunks carry, deleting
them for a re-chunk, and recording which model embedded them. Qdrant is
elsewhere: nothing here touches a vector.
"""

from typing import Any, Sequence, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DocumentChunkORM, DocumentORM
from backend.db.repositories._identifiers import parse_id
from shared.models.chunking import ChunkingResult, chunk_id_for
from shared.models.document import DocumentChunk, DocumentStatus
from shared.models.retrieval import ValidatedChunk


def _to_domain(row: DocumentChunkORM) -> DocumentChunk:
    """Map the ORM row to the API contract.

    `embedding` is always None: the vector lives in Qdrant and PostgreSQL does
    not duplicate it. The persistence-only columns — `embedding_model_id`,
    `dimension`, `token_count`, `strategy_version`, `tokenizer_id` — have no
    field on the domain model, so they are not carried; M4 reads them directly
    when it needs to decide what to re-index. The provenance a citation needs
    (ADR-0007 §1) is carried.
    """
    return DocumentChunk(
        id=str(row.id),
        document_id=str(row.document_id),
        content=row.content,
        embedding=None,
        chunk_index=row.chunk_index,
        section=row.section,
        page_start=row.page_start,
        page_end=row.page_end,
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
        chunking: ChunkingResult,
    ) -> list[DocumentChunk]:
        """Append a document's chunks, with their provenance. The one write path.

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
                # Derived, not drawn (ADR-0013 §4). This id is also the chunk's
                # Qdrant point id, so re-running the pipeline over the same
                # document writes the same points instead of a second set.
                id=chunk_id_for(
                    document_id=str(owned),
                    chunk_index=chunk.chunk_index,
                    strategy_version=chunking.strategy_version,
                    tokenizer_id=chunking.tokenizer_id,
                ),
                document_id=owned,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                section=chunk.section,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                token_count=chunk.token_count,
                strategy_version=chunking.strategy_version,
                tokenizer_id=chunking.tokenizer_id,
                chunk_metadata={},
            )
            for chunk in chunking.chunks
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

    async def provenance_for_document(
        self, document_id: str, user_id: str
    ) -> set[tuple[str, str]]:
        """Which (strategy, tokenizer) pairs this document's chunks were made by.

        A **set**, not one pair, because "all of them agree" is the thing the
        embed stage has to check rather than assume (ADR-0013 §2). Empty for a
        document with no chunks, or one this user does not own. More than one
        pair means a previous run was interrupted between two versions, and the
        document is re-chunked exactly as a stale one is.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return set()

        result = await self._session.execute(
            select(DocumentChunkORM.strategy_version, DocumentChunkORM.tokenizer_id)
            .join(DocumentORM, DocumentChunkORM.document_id == DocumentORM.id)
            .where(
                DocumentChunkORM.document_id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .distinct()
        )
        return {(strategy, tokenizer) for strategy, tokenizer in result.all()}

    async def delete_for_document(self, document_id: str, user_id: str) -> int:
        """Remove a document's chunks, so it can be chunked again.

        Owner-scoped through a subquery on `documents`, for the same reason
        every read here is: the predicate belongs in the SQL, not in an `if`
        above it. Returns how many rows went. Does not commit.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return 0

        owned = (
            select(DocumentORM.id)
            .where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
        result = await self._session.execute(
            delete(DocumentChunkORM).where(DocumentChunkORM.document_id == owned)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def mark_embedded(
        self,
        document_id: str,
        user_id: str,
        *,
        embedding_model_id: str,
        dimension: int,
    ) -> int:
        """Record which model embedded this document's chunks, and how wide.

        Written in the same transaction that sets `ready` (ADR-0013 §3): a
        document that says it is searchable must also say what made it
        searchable, or a later model change could not tell what to re-index.
        Returns how many rows were marked. Does not commit.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return 0

        owned = (
            select(DocumentORM.id)
            .where(
                DocumentORM.id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
        result = await self._session.execute(
            update(DocumentChunkORM)
            .where(DocumentChunkORM.document_id == owned)
            .values(embedding_model_id=embedding_model_id, dimension=dimension)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def validate_for_retrieval(
        self,
        chunk_ids: Sequence[str],
        user_id: str,
        *,
        embedding_model_id: str,
        dimension: int,
    ) -> dict[str, ValidatedChunk]:
        """Resolve candidate chunk ids to rows this user may actually retrieve.

        ADR-0003 §5's *correct path*, and `principles.md` §3's "returned chunk
        ids are re-fetched from PostgreSQL and any not owned by the principal
        are dropped" — as one statement, for the whole batch.

        The caller passes **ids and nothing else**. Every other fact — which
        document a chunk belongs to, who owns it, whether it still exists,
        whether it is searchable, what its text says — is read here, from the
        database. A Qdrant payload claiming a different owner or a different
        document is not compared against anything, because nothing it says is
        consulted (ADR-0014 §1).

        Five conditions, all in the SQL predicate rather than in a loop above
        it:

        * the chunk exists;
        * its document belongs to `user_id`;
        * its document is not soft-deleted;
        * its document is `ready` — the only searchable status, and vectors
          outlive documents by design (ADR-0013), so existence proves nothing;
        * it was embedded by **this** model at this width, because a score
          against a vector from another space is not a ranking.

        Returns a mapping from chunk id to the validated row, so the caller can
        preserve the candidate order the vector store returned (ADR-0014 §5). Ids that
        fail any condition are simply absent — "not yours", "not there" and
        "not searchable" are one answer, as everywhere else here. Reads only;
        does not commit.
        """
        wanted = {chunk: parse_id(chunk) for chunk in chunk_ids}
        targets = [parsed for parsed in wanted.values() if parsed is not None]
        owner = parse_id(user_id)
        if not targets or owner is None:
            return {}

        result = await self._session.execute(
            select(DocumentChunkORM, DocumentORM.id)
            .join(DocumentORM, DocumentChunkORM.document_id == DocumentORM.id)
            .where(
                DocumentChunkORM.id.in_(targets),
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
                DocumentORM.status == DocumentStatus.READY,
                DocumentChunkORM.embedding_model_id == embedding_model_id,
                DocumentChunkORM.dimension == dimension,
            )
        )

        validated: dict[str, ValidatedChunk] = {}
        for row, document_id in result.all():
            if row.page_start is None or row.page_end is None:
                # NOT NULL since migration 0006; a row without pages could not
                # be cited, and a result that cannot be cited is not a result.
                continue
            validated[str(row.id)] = ValidatedChunk(
                chunk_id=str(row.id),
                document_id=str(document_id),
                content=row.content,
                chunk_index=row.chunk_index,
                section=row.section,
                page_start=row.page_start,
                page_end=row.page_end,
                embedding_model_id=embedding_model_id,
            )
        return validated
