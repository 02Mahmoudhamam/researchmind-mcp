"""Document lifecycle management service.

The orchestration boundary: use cases live here, SQL lives in the repositories,
and this is where a transaction ends.

Ownership is *not* re-checked here. Every repository call below passes `user_id`
into a method whose SQL carries it as a predicate, so a Python check in this
layer would be a second, weaker copy of a guarantee the database already makes —
and the kind of copy that drifts. See docs/development/repositories.md.
"""

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import DocumentRepository
from shared.models.document import Document


class DocumentService:
    """Application use cases for documents.

    Takes a session rather than building one. A service that constructed its own
    session could not participate in a caller's transaction, which is exactly
    what ADR-0003's cross-store deletion order needs it to do.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._documents = DocumentRepository(session)

    async def upload_and_process(self, file: UploadFile, user_id: str) -> Document:
        """Save file, create document record, trigger processing pipeline."""
        # TODO(M3): validate the upload, write the bytes through the Storage
        # protocol (ADR-0008), create the PENDING row via self._documents.create,
        # commit, then enqueue the ARQ ingestion job (ADR-0009) and return 202.
        # The DocumentProcessingPipeline is constructed there, not here: nothing
        # else in this service needs a PDF parser, a chunker or a Qdrant client.
        ...

    async def get_document(self, document_id: str, user_id: str) -> Document | None:
        """Return the document if this user owns it.

        None covers all of: no such document, someone else's, and soft-deleted.
        Deliberately indistinguishable — a caller able to tell them apart would
        be an existence oracle for other people's corpora.

        A read, so there is nothing to commit.
        """
        return await self._documents.get_for_user(document_id, user_id)

    async def list_user_documents(self, user_id: str) -> list[Document]:
        """Return this user's documents, newest first, excluding deleted ones."""
        return await self._documents.list_for_user(user_id)

    async def delete_document(self, document_id: str, user_id: str) -> bool:
        """Soft-delete the document if this user owns it.

        **This is the transaction boundary.** The repository stamps `deleted_at`
        and flushes; this method commits, so the write is durable at the point
        the use case says it is rather than whenever a request happens to end.

        On failure the session is rolled back here and the exception re-raised.
        The request dependency would also roll back, but doing it here leaves the
        session usable for a caller that catches the error — a session left in a
        failed transaction rejects every subsequent statement with
        PendingRollbackError, which is a confusing way to discover the first
        failure.

        Commits only when a row actually changed: a call that matched nothing has
        nothing to make durable.

        ADR-0003 fixes the full order as soft-delete in PostgreSQL, committed
        first, then delete vectors in Qdrant. This is the first half. The second
        is M4, and it goes *after* this commit — which is why the commit lives
        here rather than in a request-teardown hook that could not sequence it.
        """
        try:
            deleted = await self._documents.soft_delete_for_user(document_id, user_id)
            if deleted:
                await self._session.commit()
            return deleted
        except Exception:
            await self._session.rollback()
            raise
