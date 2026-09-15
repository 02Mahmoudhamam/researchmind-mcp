"""Document lifecycle management service.

The orchestration boundary: use cases live here, SQL lives in the repositories,
and this is where a transaction ends.

Ownership is *not* re-checked here. Every repository call below passes `user_id`
into a method whose SQL carries it as a predicate, so a Python check in this
layer would be a second, weaker copy of a guarantee the database already makes —
and the kind of copy that drifts. See docs/development/repositories.md.

**Identity arrives as a `Principal`, never as a `user_id`** (M2/S2.5,
principles.md §2). Until then every method here took a bare string, which
meant nothing in the signature distinguished "the authenticated caller" from
"an id somebody passed in" — a route that forwarded `request.query_params["user_id"]`
would have type-checked and shipped. A `Principal` can only be produced by the
resolver in `backend/security/authentication.py`, so a method that requires one
cannot be handed an identity the server did not establish.

The repositories are unchanged. They still take `user_id: str`, because the
ownership predicate is SQL and SQL needs the value; this layer is where the
trusted identity is unwrapped into it:

    Principal  ->  DocumentService  ->  principal.user_id  ->  repository  ->  WHERE user_id = :owner

Authorisation is not checked here either. Whether this caller's *role* may read
or delete documents is decided by the route's permission dependency before the
service is reached; this layer answers only "which rows are theirs".
"""

from typing import BinaryIO

import anyio.to_thread
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import get_settings
from backend.db.repositories import DocumentRepository
from document_processing.validation import (
    UploadRejected,
    display_filename,
    read_capped,
    validate_pdf,
)
from shared.interfaces.ingestion import IngestionQueue
from shared.interfaces.storage import Storage, StorageError, document_storage_key
from shared.models.document import Document, DocumentType, UploadOutcome
from shared.models.ingestion import IngestionJob
from shared.models.principal import Principal
from shared.utils.logger import get_logger

_log = get_logger(__name__)

# The partial unique index behind ADR-0010. Named so a duplicate upload is told
# apart from every other integrity failure by which constraint fired.
_CONTENT_UNIQUE_INDEX = "uq_documents_user_id_content_hash"


class UploadNotConfigured(RuntimeError):
    """This service instance was built without storage or an ingestion queue.

    A wiring error, raised before anything is read or written. The API provider
    always supplies both; a service constructed for reads alone does not.
    """


class IngestionUnavailable(Exception):
    """The document was recorded, but no ingestion job could be queued for it.

    Raised after the upload has been undone: the new document soft-deleted and
    committed, and the stored file removed if this upload created it. Leaving
    the document `pending` with no job behind it would strand it — nothing would
    ever move it, and a retried upload would only return it again (ADR-0010).
    Undoing it means the client's retry starts from nothing and succeeds once
    the queue is back.
    """


class UploadPersistenceFailed(Exception):
    """The database could not record an upload. No document exists for it.

    Raised after the transaction is rolled back and any file *this upload*
    stored has been removed. The original exception is chained.
    """


def _is_duplicate_content(exc: IntegrityError) -> bool:
    """True when the violated constraint is the per-owner content index."""
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    if name is not None:
        return bool(name == _CONTENT_UNIQUE_INDEX)
    return _CONTENT_UNIQUE_INDEX in str(exc.orig)


class DocumentService:
    """Application use cases for documents.

    Takes a session rather than building one. A service that constructed its own
    session could not participate in a caller's transaction, which is exactly
    what ADR-0003's cross-store deletion order needs it to do.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: Storage | None = None,
        ingestion: IngestionQueue | None = None,
    ) -> None:
        """Storage and the ingestion queue are needed by upload alone.

        Optional so reads and deletes do not have to invent collaborators they
        never use; `upload_and_process` refuses to start without both.
        """
        self._session = session
        self._documents = DocumentRepository(session)
        self._storage = storage
        self._ingestion = ingestion

    async def upload_and_process(
        self, *, filename: str | None, stream: BinaryIO, principal: Principal
    ) -> UploadOutcome:
        """Validate, store and record an upload, then hand it to ingestion.

        The order is the design:

        1. **Validate** (principles.md §4) — filename, size cap while reading,
           magic bytes, opens, not password-protected, page cap. A refusal
           happens here, before a byte is stored or a row exists.
        2. **Deduplicate** (ADR-0010) — if this owner already has a live
           document with this content, return it. Nothing else happens.
        3. **Store** at `{owner}/{sha256}.pdf` (ADR-0008). Before the database,
           so a committed row never points at bytes that were not written.
        4. **Record** the document as `pending` and commit. On failure the
           transaction is rolled back and the stored file removed — *only if
           this call created it*, because an identical file already on disk
           belongs to another row of this owner's.
        5. **Enqueue** an `IngestionJob` — after the commit, so a worker can
           never receive a job for a document that does not exist. If the queue
           refuses it, the upload is undone (M3/S3.2): a document with no job
           would stay `pending` forever.

        Filesystem and database cannot share a transaction, and this does not
        pretend otherwise. Step 4's compensation covers the ordinary failures;
        what it cannot cover — a crash between steps 3 and 4, or a concurrent
        identical upload reusing a file this call then removes — leaves at worst
        an orphaned file, which ADR-0008 accepts as wasted disk, not leaked data.

        The owner is `principal.user_id`, and nothing else. There is no
        parameter through which a caller could name another.

        :raises UploadRejected: the upload breaks a validation rule.
        :raises StorageError: the bytes could not be stored; nothing recorded.
        :raises UploadPersistenceFailed: the row could not be recorded; nothing
            left behind that this call created.
        :raises IngestionUnavailable: recorded, but could not be queued; the
            upload has been undone.
        :raises UploadNotConfigured: built without storage or a queue.
        """
        if self._storage is None or self._ingestion is None:
            raise UploadNotConfigured("upload requires storage and an ingestion queue")
        storage, ingestion = self._storage, self._ingestion
        settings = get_settings()
        owner_id = principal.user_id

        try:
            name = display_filename(filename)
            # Blocking disk reads and PyMuPDF are both kept off the event loop
            # (ADR-0009 §3).
            data = await anyio.to_thread.run_sync(
                read_capped, stream, settings.MAX_UPLOAD_BYTES
            )
            validated = await anyio.to_thread.run_sync(
                validate_pdf, data, settings.MAX_PDF_PAGES
            )
        except UploadRejected as rejection:
            # The reason code, never the filename or any content.
            _log.info(
                "document.upload.rejected",
                owner_id=owner_id,
                reason=rejection.reason.value,
            )
            raise

        existing = await self._documents.find_live_by_content_hash_for_user(
            validated.content_hash, owner_id
        )
        if existing is not None:
            _log.info(
                "document.upload.deduplicated",
                document_id=existing.id,
                owner_id=owner_id,
                content_hash=validated.content_hash,
            )
            return UploadOutcome(document=existing, created=False)

        key = document_storage_key(owner_id, validated.content_hash)
        try:
            created_blob = await storage.put(key, data)
        except StorageError:
            _log.error(
                "document.upload.storage_failed",
                owner_id=owner_id,
                content_hash=validated.content_hash,
            )
            raise

        try:
            document = await self._documents.create(
                user_id=owner_id,
                filename=name,
                doc_type=DocumentType.PDF,
                storage_key=key,
                content_hash=validated.content_hash,
                size_bytes=validated.size_bytes,
                mime_type=validated.mime_type,
                page_count=validated.page_count,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_content(exc):
                # A concurrent upload of the same file by the same owner won the
                # insert. Its row points at the file on disk, so the file stays,
                # whoever wrote it — and this upload's answer is that document.
                winner = await self._documents.find_live_by_content_hash_for_user(
                    validated.content_hash, owner_id
                )
                if winner is not None:
                    return UploadOutcome(document=winner, created=False)
            await self._discard(storage, key, created_blob, owner_id)
            raise UploadPersistenceFailed("the document could not be recorded") from exc
        except Exception as exc:
            await self._session.rollback()
            await self._discard(storage, key, created_blob, owner_id)
            raise UploadPersistenceFailed("the document could not be recorded") from exc

        _log.info(
            "document.upload.stored",
            document_id=document.id,
            owner_id=owner_id,
            content_hash=validated.content_hash,
            size_bytes=validated.size_bytes,
            page_count=validated.page_count,
            status=document.status.value,
            created_blob=created_blob,
        )

        try:
            await ingestion.enqueue(
                IngestionJob(
                    document_id=document.id,
                    owner_id=owner_id,
                    storage_key=key,
                    content_hash=validated.content_hash,
                    mime_type=validated.mime_type,
                )
            )
        except Exception as exc:
            await self._undo_unqueued_upload(
                document.id, owner_id, storage, key, created_blob
            )
            raise IngestionUnavailable("no ingestion job could be queued") from exc
        return UploadOutcome(document=document, created=True)

    async def _undo_unqueued_upload(
        self,
        document_id: str,
        owner_id: str,
        storage: Storage,
        key: str,
        created_blob: bool,
    ) -> None:
        """Take back a committed upload whose ingestion job could not be queued.

        Soft delete rather than a hard delete: repositories offer no other kind
        (ADR-0003), and a soft-deleted row neither counts for ADR-0010's
        duplicate rule nor appears to its owner. If the enqueue in fact reached
        Redis before failing, the worker finds the document deleted and rejects
        the job — so this is safe whichever way the failure really went.

        Best effort, like `_discard`: if undoing fails as well, it is logged,
        and the error the caller sees is still the queue's.
        """
        _log.error(
            "document.upload.enqueue_failed", document_id=document_id, owner_id=owner_id
        )
        try:
            await self._documents.soft_delete_for_user(document_id, owner_id)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            _log.error(
                "document.upload.unqueued_document_left",
                document_id=document_id,
                owner_id=owner_id,
            )
            return
        await self._discard(storage, key, created_blob, owner_id)

    @staticmethod
    async def _discard(
        storage: Storage, key: str, created_blob: bool, owner_id: str
    ) -> None:
        """Remove a file this upload stored, after its row failed to commit.

        Only when `created_blob` is True. A file that was already there is an
        identical upload's — a live or soft-deleted document of this owner still
        points at it — and deleting it would break that document to tidy up
        after this one.

        Best effort: a failure to delete is logged, not raised, so it cannot
        replace the database error the caller actually needs to see.
        """
        if not created_blob:
            return
        try:
            await storage.delete(key)
        except StorageError:
            _log.error(
                "document.upload.orphaned_blob", owner_id=owner_id, storage_key=key
            )

    async def get_document(
        self, document_id: str, principal: Principal
    ) -> Document | None:
        """Return the document if this principal owns it.

        None covers all of: no such document, someone else's, and soft-deleted.
        Deliberately indistinguishable — a caller able to tell them apart would
        be an existence oracle for other people's corpora.

        A read, so there is nothing to commit.
        """
        return await self._documents.get_for_user(document_id, principal.user_id)

    async def list_user_documents(self, principal: Principal) -> list[Document]:
        """Return this principal's documents, newest first, excluding deleted ones."""
        return await self._documents.list_for_user(principal.user_id)

    async def delete_document(self, document_id: str, principal: Principal) -> bool:
        """Soft-delete the document if this principal owns it.

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
            deleted = await self._documents.soft_delete_for_user(
                document_id, principal.user_id
            )
            if deleted:
                await self._session.commit()
            return deleted
        except Exception:
            await self._session.rollback()
            raise
