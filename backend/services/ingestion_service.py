"""Ingestion — what happens to an uploaded document after the request is gone.

ADR-0009 runs this in a worker. The worker has no request, no token and no
Principal; it has an `IngestionJob` built by the request that authenticated the
upload, and the database. This service decides what that job is allowed to do,
and records the outcome. It knows nothing about ARQ, HTTP or which storage
backend holds the bytes.

The order of a delivery, and why each step is where it is:

1. **Read the document owner-scoped**, using the job's `owner_id`. A job naming
   the wrong owner reads nothing, and the document is never touched.
2. **Check the job against the row** — owner, storage key, content hash, MIME
   type. A job that disagrees with the database is *rejected*: it is the job
   that is wrong, so the owner's document is left exactly as it was.
3. **Check the state.** `processing` means another delivery holds the claim;
   `ready` and `failed` are terminal. Either way, nothing is done.
4. **Claim**: `pending → processing`, a compare-and-set, committed. Only one
   delivery can win it.
5. **Verify the bytes** through `Storage`: present, readable, and hashing to
   the recorded SHA-256. A document whose bytes are wrong *fails*, with the
   reason persisted, because that document can never be processed.
6. **Release**: `processing → pending`. There is no processing stage yet — text
   extraction and chunking are later M3 sprints — and `ready` means the content
   has been processed (ADR-0009, the M3 definition of done). The only true
   state for a sound document nothing has processed is `pending`.

Failures after the claim move `processing → failed`, which is the lifecycle
ADR-0009 §4 draws: a document fails *while being processed*, never straight out
of `pending`.

Every transition is a single conditional UPDATE, committed here. Repositories
never commit (M1/S1.4).
"""

import asyncio

import anyio.to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import DocumentRepository
from document_processing.validation import PDF_MIME_TYPE, content_hash
from shared.interfaces.storage import (
    Storage,
    StorageError,
    StorageObjectMissing,
    document_storage_key,
)
from shared.models.document import DocumentStatus
from shared.models.ingestion import (
    IngestionJob,
    IngestionOutcome,
    IngestionReason,
    IngestionResult,
    IngestionTarget,
)
from shared.utils.logger import get_logger

_log = get_logger(__name__)


class TransientIngestionError(Exception):
    """This delivery could not finish for a reason that may not recur.

    The document has been put back to `pending` if this delivery had claimed
    it, so a retry starts clean. The worker turns this into a delayed retry; it
    is never raised on a final attempt — that records a failure instead
    (ADR-0009 §5, "never infinite retry").
    """

    def __init__(self, reason: IngestionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class IngestionService:
    """Runs one delivery of an ingestion job against the database and storage."""

    def __init__(self, session: AsyncSession, storage: Storage) -> None:
        self._session = session
        self._storage = storage
        self._documents = DocumentRepository(session)

    async def ingest(
        self, job: IngestionJob, *, final_attempt: bool
    ) -> IngestionResult:
        """Execute one delivery. Returns what happened; raises only to retry.

        :param final_attempt: no retry will follow. A transient failure is then
            recorded as a terminal failure rather than raised.
        :raises TransientIngestionError: a retryable failure, not on the final
            attempt. The claim, if taken, has been released.
        """
        # A job is only as trustworthy as its parts agree with each other. The
        # storage key is derived, not chosen: if it is not what the owner and
        # hash derive to, the job was not built by the upload service.
        try:
            derived_key = document_storage_key(job.owner_id, job.content_hash)
        except ValueError:
            return self._rejected(job, IngestionReason.INVALID_JOB)
        if derived_key != job.storage_key:
            return self._rejected(job, IngestionReason.STORAGE_KEY_MISMATCH)

        target = await self._documents.get_ingestion_target_for_user(
            job.document_id, job.owner_id
        )
        if target is None:
            # Owner-scoped read came back empty. Distinguish, for operators only,
            # a routine case (deleted or never existed) from a job that names
            # the wrong owner — which should not happen and is worth noticing.
            exists = await self._documents.live_document_exists(job.document_id)
            await self._session.rollback()
            return self._rejected(
                job,
                (
                    IngestionReason.OWNERSHIP_MISMATCH
                    if exists
                    else IngestionReason.DOCUMENT_NOT_FOUND
                ),
            )

        mismatch = self._job_disagrees_with(job, target)
        if mismatch is not None:
            await self._session.rollback()
            return self._rejected(job, mismatch)

        if target.status is DocumentStatus.PROCESSING:
            await self._session.rollback()
            return self._skipped(job, IngestionOutcome.SKIPPED_IN_PROGRESS)
        if target.status in (DocumentStatus.READY, DocumentStatus.FAILED):
            await self._session.rollback()
            return self._skipped(job, IngestionOutcome.SKIPPED_TERMINAL)

        if not await self._transition(
            job, expected=DocumentStatus.PENDING, new=DocumentStatus.PROCESSING
        ):
            # Lost the race to another delivery, or the document moved on.
            return self._skipped(job, IngestionOutcome.SKIPPED_IN_PROGRESS)
        _log.info(
            "ingestion.claimed", document_id=job.document_id, owner_id=job.owner_id
        )

        try:
            return await self._verify_claimed(job, target, final_attempt=final_attempt)
        except asyncio.CancelledError:
            # The job was cancelled — a timeout or a worker shutting down. Put the
            # document back so the next delivery can take it, rather than leaving
            # it for the reaper to fail. Shielded, because this coroutine is
            # already being cancelled.
            await asyncio.shield(self._release_after_interruption(job))
            raise

    async def _verify_claimed(
        self, job: IngestionJob, target: IngestionTarget, *, final_attempt: bool
    ) -> IngestionResult:
        """Everything done while holding the claim."""
        if target.mime_type != PDF_MIME_TYPE:
            return await self._fail(job, IngestionReason.UNSUPPORTED_MIME_TYPE)

        try:
            content = await self._storage.get(job.storage_key)
        except StorageObjectMissing:
            return await self._fail(job, IngestionReason.STORAGE_MISSING)
        except StorageError:
            return await self._transient(
                job, IngestionReason.STORAGE_READ_FAILURE, final_attempt=final_attempt
            )
        except Exception:
            return await self._transient(
                job, IngestionReason.PROCESSING_FAILURE, final_attempt=final_attempt
            )

        # SHA-256 of a large file is CPU work; keep it off the event loop.
        actual = await anyio.to_thread.run_sync(content_hash, content)
        if actual != target.content_hash:
            # The bytes are not the bytes that were validated. Corruption or
            # tampering — either way permanent, and never retried.
            return await self._fail(job, IngestionReason.CONTENT_HASH_MISMATCH)

        # No processing stage exists yet (M3/S3.2). Release the claim.
        if not await self._transition(
            job, expected=DocumentStatus.PROCESSING, new=DocumentStatus.PENDING
        ):
            return self._claim_lost(job)
        _log.info(
            "ingestion.verified",
            document_id=job.document_id,
            owner_id=job.owner_id,
            content_hash=job.content_hash,
            status=DocumentStatus.PENDING.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.VERIFIED, document_id=job.document_id
        )

    async def reap_stale_processing(self, *, stale_after_seconds: int) -> list[str]:
        """Fail documents whose claim has outlived any possible worker (ADR-0009 §6).

        The caller supplies a threshold greater than the job timeout; Settings
        refuses any other configuration. Commits, and returns the ids reaped.
        """
        try:
            reaped = await self._documents.fail_stale_processing(
                stale_after_seconds=stale_after_seconds,
                failure_reason=IngestionReason.STALE_PROCESSING.value,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        for document_id in reaped:
            _log.warning(
                "ingestion.reaped",
                document_id=document_id,
                reason=IngestionReason.STALE_PROCESSING.value,
            )
        return reaped

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _job_disagrees_with(
        job: IngestionJob, target: IngestionTarget
    ) -> IngestionReason | None:
        """The first field on which the job and the database disagree, if any.

        The owner is compared again even though the read was already scoped to
        it — the scoped read is what makes a wrong owner harmless; this makes it
        visible if that read is ever loosened.
        """
        if target.owner_id != job.owner_id:
            return IngestionReason.OWNERSHIP_MISMATCH
        # Hash before key: the key is derived from the hash, so a wrong hash
        # always yields a wrong key too, and the hash is the more precise
        # account of what the job got wrong.
        if target.content_hash != job.content_hash:
            return IngestionReason.CONTENT_HASH_MISMATCH
        if target.storage_key != job.storage_key:
            return IngestionReason.STORAGE_KEY_MISMATCH
        if target.mime_type != job.mime_type:
            return IngestionReason.MIME_TYPE_MISMATCH
        return None

    async def _transition(
        self,
        job: IngestionJob,
        *,
        expected: DocumentStatus,
        new: DocumentStatus,
        failure_reason: IngestionReason | None = None,
    ) -> bool:
        """One committed compare-and-set, scoped to the job's owner."""
        try:
            moved = await self._documents.transition_status_for_user(
                job.document_id,
                job.owner_id,
                expected=expected,
                new=new,
                failure_reason=failure_reason.value if failure_reason else None,
            )
            await self._session.commit()
            return moved
        except Exception:
            await self._session.rollback()
            raise

    async def _fail(
        self, job: IngestionJob, reason: IngestionReason
    ) -> IngestionResult:
        """Persist `processing → failed` with the reason."""
        if not await self._transition(
            job,
            expected=DocumentStatus.PROCESSING,
            new=DocumentStatus.FAILED,
            failure_reason=reason,
        ):
            return self._claim_lost(job)
        _log.warning(
            "ingestion.failed",
            document_id=job.document_id,
            owner_id=job.owner_id,
            reason=reason.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.FAILED, document_id=job.document_id, reason=reason
        )

    async def _transient(
        self, job: IngestionJob, reason: IngestionReason, *, final_attempt: bool
    ) -> IngestionResult:
        """Retry later — or, on the last attempt, fail for good."""
        if final_attempt:
            return await self._fail(job, reason)
        released = await self._transition(
            job, expected=DocumentStatus.PROCESSING, new=DocumentStatus.PENDING
        )
        if not released:
            return self._claim_lost(job)
        _log.info(
            "ingestion.retry",
            document_id=job.document_id,
            owner_id=job.owner_id,
            reason=reason.value,
        )
        raise TransientIngestionError(reason)

    async def _release_after_interruption(self, job: IngestionJob) -> None:
        try:
            await self._transition(
                job, expected=DocumentStatus.PROCESSING, new=DocumentStatus.PENDING
            )
            _log.info("ingestion.interrupted", document_id=job.document_id)
        except Exception:
            # The database is unreachable too. The reaper will find the claim.
            _log.error("ingestion.release_failed", document_id=job.document_id)

    @staticmethod
    def _rejected(job: IngestionJob, reason: IngestionReason) -> IngestionResult:
        # Rejections are logged at error: a sound system does not produce jobs
        # that disagree with the database, so each one is worth a look.
        _log.error(
            "ingestion.rejected",
            document_id=job.document_id,
            owner_id=job.owner_id,
            reason=reason.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.REJECTED,
            document_id=job.document_id,
            reason=reason,
        )

    @staticmethod
    def _skipped(job: IngestionJob, outcome: IngestionOutcome) -> IngestionResult:
        _log.info(
            "ingestion.skipped", document_id=job.document_id, outcome=outcome.value
        )
        return IngestionResult(outcome=outcome, document_id=job.document_id)

    @staticmethod
    def _claim_lost(job: IngestionJob) -> IngestionResult:
        _log.warning("ingestion.claim_lost", document_id=job.document_id)
        return IngestionResult(
            outcome=IngestionOutcome.CLAIM_LOST, document_id=job.document_id
        )
