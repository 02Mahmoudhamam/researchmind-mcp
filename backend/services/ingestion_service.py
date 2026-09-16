"""Ingestion — what happens to an uploaded document after the request is gone.

ADR-0009 runs this in a worker. The worker has no request, no token and no
Principal; it has an `IngestionJob` built by the request that authenticated the
upload, and the database. This service decides what that job is allowed to do,
and records the outcome. It knows nothing about ARQ, HTTP, which storage backend
holds the bytes, or which library reads a PDF.

The order of a delivery, and why each step is where it is:

1. **Read the document owner-scoped**, using the job's `owner_id`. A job naming
   the wrong owner reads nothing, and the document is never touched.
2. **Check the job against the row** — owner, storage key, content hash, MIME
   type. A job that disagrees with the database is *rejected*: it is the job
   that is wrong, so the owner's document is left exactly as it was.
3. **Check the state.** `processing` means another delivery holds the claim;
   `parsed` means this job's work is done; `ready` and `failed` are terminal.
   In every one of those, nothing is done.
4. **Claim**: `pending → processing`, a compare-and-set, committed. Only one
   delivery can win it — which is why the claim comes before reading the
   bytes: concurrent deliveries never read, hash or parse the same document.
5. **Verify the bytes** through `Storage`: present, readable, and hashing to
   the recorded SHA-256. A document whose bytes are wrong *fails*, with the
   reason persisted, because that document can never be processed.
6. **Extract the text** (M3/S3.3) — only now, from bytes proven to be the
   bytes validated at upload; never before. Through `PdfTextExtractor`, which
   bounds the time a hostile PDF can take.
7. **Store and advance**: the pages and `processing → parsed`, in one
   transaction. A document is never `parsed` without its pages, and never has
   pages while it is not.
8. **Chunk** (M3/S3.4): claim `parsed → processing`, read the pages back
   owner-scoped, split them into section-aware chunks, and store those with
   `processing → chunked` in one transaction. `ready` means searchable
   (ADR-0009, the M3 definition of done) and chunks are not searchable until
   M4 embeds them — so `chunked`, not `ready`.

A delivery runs every stage the document is ready for: an upload is parsed and
then chunked by the same job, and a job that finds a `parsed` document chunks
it. Each stage is its own claim, its own transaction and its own retry, so a
failure in one never undoes the other.

Failures after the claim move `processing → failed`, which is the lifecycle
ADR-0009 §4 draws: a document fails *while being processed*, never straight out
of `pending`.

Every transition is a single conditional UPDATE, committed here. Repositories
never commit (M1/S1.4).

Two maintenance passes run beside deliveries: the reaper, which fails claims no
worker holds any more (S3.2), and the recovery sweep, which re-enqueues `pending`
documents that no job will pick up (S3.3).
"""

import asyncio
from dataclasses import dataclass
from typing import Sequence

import anyio.to_thread
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import (
    DocumentChunkRepository,
    DocumentPageRepository,
    DocumentRepository,
)
from document_processing.validation import PDF_MIME_TYPE, content_hash
from shared.interfaces.chunking import DocumentChunker
from shared.interfaces.ingestion import IngestionQueue
from shared.interfaces.pdf_extraction import PdfExtractionError, PdfTextExtractor
from shared.interfaces.storage import (
    Storage,
    StorageError,
    StorageObjectMissing,
    document_storage_key,
)
from shared.models.document import DocumentStatus
from shared.models.chunking import ChunkingResult
from shared.models.extraction import ExtractedPage, ExtractedText
from shared.models.ingestion import (
    IngestionJob,
    IngestionOutcome,
    IngestionReason,
    IngestionResult,
    IngestionTarget,
    RecoveryResult,
)
from shared.utils.logger import get_logger

_log = get_logger(__name__)

# The statuses a delivery can still advance: where the recovery sweep looks, and
# what a final attempt claims before recording that it gave up. `parsed` joined
# `pending` in M3/S3.4 — a document whose job died between parsing and chunking
# is as stranded as one whose job never reached Redis.
RECOVERABLE_STATUSES = (DocumentStatus.PENDING, DocumentStatus.PARSED)


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


@dataclass(frozen=True)
class _Stage:
    """One step of the pipeline: what it is called, and where it starts from."""

    name: str
    starts_at: DocumentStatus


# Parsing takes a `pending` document; chunking takes a `parsed` one. A stage
# releases its claim back to where it started, so a retry begins where this
# attempt did (M3/S3.4).
_PARSE = _Stage("parse", DocumentStatus.PENDING)
_CHUNK = _Stage("chunk", DocumentStatus.PARSED)


class IngestionService:
    """Runs one delivery of an ingestion job against the database and storage."""

    def __init__(
        self,
        session: AsyncSession,
        storage: Storage,
        *,
        extractor: PdfTextExtractor,
        chunker: DocumentChunker,
        max_pages: int,
    ) -> None:
        """
        :param extractor: reads text from verified PDF bytes, within its own
            time budget.
        :param chunker: splits a parsed document's pages into chunks.
        :param max_pages: the most pages a document may have — the upload cap,
            enforced again here because stored bytes and old rows predate any
            later change to it.
        """
        self._session = session
        self._storage = storage
        self._extractor = extractor
        self._chunker = chunker
        self._max_pages = max_pages
        self._documents = DocumentRepository(session)
        self._pages = DocumentPageRepository(session)
        self._chunks = DocumentChunkRepository(session)

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
        if target.status is DocumentStatus.CHUNKED:
            await self._session.rollback()
            return self._skipped(job, IngestionOutcome.SKIPPED_CHUNKED)
        if target.status in (DocumentStatus.READY, DocumentStatus.FAILED):
            await self._session.rollback()
            return self._skipped(job, IngestionOutcome.SKIPPED_TERMINAL)

        if target.status is DocumentStatus.PENDING:
            parsed = await self._run(
                job,
                target,
                stage=_PARSE,
                final_attempt=final_attempt,
            )
            if parsed.outcome is not IngestionOutcome.PARSED:
                return parsed
            # Parsed by this delivery, so the next stage is this delivery's too:
            # waiting for the sweep to hand the document back would be a minute
            # of latency for work already in hand.
        return await self._run(job, target, stage=_CHUNK, final_attempt=final_attempt)

    async def _run(
        self,
        job: IngestionJob,
        target: IngestionTarget,
        *,
        stage: "_Stage",
        final_attempt: bool,
    ) -> IngestionResult:
        """Claim the document for one stage, run it, and release or advance.

        The claim is a compare-and-set from the status this stage starts at, so
        only one delivery runs a stage, and a document that has moved on is
        skipped rather than reprocessed.
        """
        if not await self._transition(
            job, expected=stage.starts_at, new=DocumentStatus.PROCESSING
        ):
            # Lost the race to another delivery, or the document moved on.
            return self._skipped(job, IngestionOutcome.SKIPPED_IN_PROGRESS)
        _log.info(
            "ingestion.claimed",
            document_id=job.document_id,
            owner_id=job.owner_id,
            stage=stage.name,
        )

        try:
            if stage is _PARSE:
                return await self._process_claimed(
                    job, target, final_attempt=final_attempt
                )
            return await self._chunk_claimed(job, final_attempt=final_attempt)
        except asyncio.CancelledError:
            # The job was cancelled — a timeout or a worker shutting down. Put the
            # document back so the next delivery can take it, rather than leaving
            # it for the reaper to fail. Shielded, because this coroutine is
            # already being cancelled.
            await asyncio.shield(self._release_after_interruption(job, stage.starts_at))
            raise

    async def _process_claimed(
        self, job: IngestionJob, target: IngestionTarget, *, final_attempt: bool
    ) -> IngestionResult:
        """Everything done while holding the claim: verify, extract, store."""
        if target.mime_type != PDF_MIME_TYPE:
            return await self._fail(job, IngestionReason.UNSUPPORTED_MIME_TYPE)
        if target.page_count is not None and target.page_count > self._max_pages:
            # Counted at upload, under a cap that may since have been lowered.
            # Refused before the bytes are even read.
            return await self._fail(job, IngestionReason.PAGE_LIMIT_EXCEEDED)

        try:
            content = await self._storage.get(job.storage_key)
        except StorageObjectMissing:
            return await self._fail(job, IngestionReason.STORAGE_MISSING)
        except StorageError:
            return await self._transient(
                job,
                IngestionReason.STORAGE_READ_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PENDING,
            )
        except Exception:
            return await self._transient(
                job,
                IngestionReason.PROCESSING_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PENDING,
            )

        # SHA-256 of a large file is CPU work; keep it off the event loop.
        actual = await anyio.to_thread.run_sync(content_hash, content)
        if actual != target.content_hash:
            # The bytes are not the bytes that were validated. Corruption or
            # tampering — either way permanent, and never retried.
            return await self._fail(job, IngestionReason.CONTENT_HASH_MISMATCH)

        # Only now are the bytes known to be the bytes validated at upload, and
        # only now may anything read them as a PDF (M3/S3.3).
        try:
            extracted = await self._extractor.extract(
                content, max_pages=self._max_pages
            )
        except PdfExtractionError as exc:
            reason = IngestionReason(exc.reason.value)
            if exc.transient:
                return await self._transient(
                    job,
                    reason,
                    final_attempt=final_attempt,
                    release_to=DocumentStatus.PENDING,
                )
            return await self._fail(job, reason)
        except Exception:
            return await self._transient(
                job,
                IngestionReason.PROCESSING_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PENDING,
            )

        if target.page_count is not None and extracted.page_count != target.page_count:
            # The same bytes counted differently at upload. Nothing downstream
            # could trust page numbers from either count.
            return await self._fail(job, IngestionReason.PAGE_COUNT_MISMATCH)
        if not extracted.has_text:
            # A valid PDF with nothing to read — typically a scan. Without OCR
            # (out of scope) it can never be searched, and a `parsed` document
            # with no text would claim otherwise.
            return await self._fail(job, IngestionReason.NO_EXTRACTABLE_TEXT)

        try:
            stored = await self._store_parsed(job, extracted)
        except Exception:
            # Rolled back: no pages and no `parsed`. The claim is released and
            # the delivery retried, or failed on its last attempt.
            return await self._transient(
                job,
                IngestionReason.PROCESSING_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PENDING,
            )
        if not stored:
            return self._claim_lost(job)
        _log.info(
            "ingestion.parsed",
            document_id=job.document_id,
            owner_id=job.owner_id,
            content_hash=job.content_hash,
            page_count=extracted.page_count,
            pages_with_text=sum(1 for page in extracted.pages if page.blocks),
            status=DocumentStatus.PARSED.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.PARSED, document_id=job.document_id
        )

    async def _store_parsed(self, job: IngestionJob, extracted: ExtractedText) -> bool:
        """The pages and `processing → parsed`: one transaction, both or neither.

        The status moves first, as a compare-and-set that also locks the row;
        the pages are written under that lock; then one commit. A claim lost to
        the reaper matches nothing, and the transaction is rolled back before a
        single page is written. Returns whether the document is now `parsed`.
        """
        try:
            moved = await self._documents.transition_status_for_user(
                job.document_id,
                job.owner_id,
                expected=DocumentStatus.PROCESSING,
                new=DocumentStatus.PARSED,
            )
            if not moved:
                await self._session.rollback()
                return False
            await self._pages.add_for_user(
                document_id=job.document_id,
                user_id=job.owner_id,
                pages=extracted.pages,
            )
            await self._session.commit()
            return True
        except Exception:
            await self._session.rollback()
            raise

    async def _chunk_claimed(
        self, job: IngestionJob, *, final_attempt: bool
    ) -> IngestionResult:
        """Everything done while holding the chunking claim (M3/S3.4)."""
        try:
            pages = await self._pages.list_for_document(job.document_id, job.owner_id)
        finally:
            await self._session.rollback()
        if not pages:
            # `parsed` says its pages were stored; they are not there now.
            # Nothing this stage can do will bring them back.
            return await self._fail(job, IngestionReason.NO_EXTRACTED_PAGES)

        try:
            # Pure Python over the whole document's text: short, but not the
            # event loop's work (the same reason hashing is offloaded).
            chunking = await anyio.to_thread.run_sync(self._chunk_pages, pages)
        except Exception:
            return await self._transient(
                job,
                IngestionReason.CHUNKING_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PARSED,
            )

        if not chunking.chunks:
            # Pages with no text a chunk could be made of. A `chunked` document
            # with nothing in it would claim work that did not happen.
            return await self._fail(job, IngestionReason.NO_CHUNKS_PRODUCED)

        try:
            stored = await self._store_chunks(job, chunking)
        except Exception:
            # Rolled back: no chunks, and not `chunked`.
            return await self._transient(
                job,
                IngestionReason.CHUNKING_FAILURE,
                final_attempt=final_attempt,
                release_to=DocumentStatus.PARSED,
            )
        if not stored:
            return self._claim_lost(job)
        _log.info(
            "ingestion.chunked",
            document_id=job.document_id,
            owner_id=job.owner_id,
            chunk_count=len(chunking.chunks),
            page_count=len(pages),
            strategy_version=chunking.strategy_version,
            tokenizer_id=chunking.tokenizer_id,
            status=DocumentStatus.CHUNKED.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.CHUNKED, document_id=job.document_id
        )

    def _chunk_pages(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
        return self._chunker.chunk(pages)

    async def _store_chunks(self, job: IngestionJob, chunking: ChunkingResult) -> bool:
        """The chunks and `processing → chunked`: one transaction, both or neither.

        The status moves first, as a compare-and-set that also locks the row and
        records `chunk_count`; the chunks are written under that lock; then one
        commit. A claim lost to the reaper matches nothing, and the transaction
        is rolled back before a single chunk is written.
        """
        try:
            moved = await self._documents.transition_status_for_user(
                job.document_id,
                job.owner_id,
                expected=DocumentStatus.PROCESSING,
                new=DocumentStatus.CHUNKED,
                chunk_count=len(chunking.chunks),
            )
            if not moved:
                await self._session.rollback()
                return False
            await self._chunks.add_many(
                document_id=job.document_id,
                user_id=job.owner_id,
                chunking=chunking,
            )
            await self._session.commit()
            return True
        except Exception:
            await self._session.rollback()
            raise

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

    async def recover_pending(
        self, queue: IngestionQueue, *, limit: int
    ) -> RecoveryResult:
        """Queue a job for every document a stage could advance but none will.

        A document can be left with no job to take it further: an ARQ job
        timeout cancels the delivery, which puts its claim back and is not
        retried; a job that exhausted its attempts on errors the database could
        not record; a Redis outage between an upload's commit and its enqueue.
        Without this, nothing would ever look at those documents again.

        `pending` (M3/S3.3) and, since M3/S3.4, `parsed`: the two statuses a
        delivery starts a stage from. Never `processing` — that claim is the
        reaper's, and a document another worker holds must not be handed a
        second job — and never `chunked`, `ready` or `failed`, which no stage
        advances.

        The job is built from the row PostgreSQL holds — its own `user_id`,
        storage key, hash and type — never from a request, and it passes the
        same checks as any other delivery when it runs. Duplicates are ARQ's to
        refuse: the job id is the document's, and a document with a job already
        queued, deferred or running gets no second one, however many sweeps race.

        :param limit: the most documents one pass considers, oldest first.
        """
        try:
            candidates = await self._documents.list_for_recovery(
                statuses=RECOVERABLE_STATUSES, limit=limit
            )
        finally:
            # A read-only transaction; end it before any network call to Redis.
            await self._session.rollback()

        requeued: list[str] = []
        already_queued = skipped = 0
        for target in candidates:
            job = self._job_from_row(target)
            if job is None:
                skipped += 1
                _log.error(
                    "ingestion.recovery.skipped",
                    document_id=target.document_id,
                    reason=IngestionReason.INVALID_JOB.value,
                )
                continue
            if await queue.enqueue(job):
                requeued.append(job.document_id)
                _log.info(
                    "ingestion.recovery.requeued",
                    document_id=job.document_id,
                    owner_id=job.owner_id,
                )
            else:
                already_queued += 1
        if requeued or skipped:
            _log.info(
                "ingestion.recovery.pass",
                requeued=len(requeued),
                already_queued=already_queued,
                skipped=skipped,
            )
        return RecoveryResult(
            requeued=tuple(requeued), already_queued=already_queued, skipped=skipped
        )

    async def record_given_up(self, job: IngestionJob) -> bool:
        """Fail a document whose final attempt ended in an unclassified error.

        Called by the worker after the last attempt raised something the
        service did not classify. Without it, a document that attempt never
        claimed would stay `pending`, and the recovery sweep would give it a
        fresh job with a fresh retry budget — retrying forever, which ADR-0009 §5
        forbids. Recorded as `processing_failure`.

        Still `processing → failed` only: a document that is `pending` or
        `parsed` is claimed first. A document in `processing` here is this
        delivery's own claim — ARQ runs at most one job per document id — or a
        dead worker's, which the reaper would fail anyway. Returns whether a
        failure was recorded. Raises if the database cannot be reached, in which
        case the sweep recovers the document once it can.
        """
        reason = IngestionReason.PROCESSING_FAILURE
        claimed = any(
            [
                await self._transition(
                    job, expected=status, new=DocumentStatus.PROCESSING
                )
                for status in RECOVERABLE_STATUSES
            ]
        )
        if not claimed and not await self._is_processing(job):
            return False
        if not await self._transition(
            job,
            expected=DocumentStatus.PROCESSING,
            new=DocumentStatus.FAILED,
            failure_reason=reason,
        ):
            return False
        _log.warning(
            "ingestion.failed",
            document_id=job.document_id,
            owner_id=job.owner_id,
            reason=reason.value,
        )
        return True

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _job_from_row(target: IngestionTarget) -> IngestionJob | None:
        """The job a recovered document gets, or None if its row cannot make one.

        Every field comes from the row. The storage key must still be what the
        row's owner and hash derive to — the check a delivery would make — so a
        row that fails it is reported rather than handed to a job certain to be
        rejected, every minute, forever.
        """
        if (
            target.storage_key is None
            or target.content_hash is None
            or target.mime_type is None
        ):
            return None
        try:
            derived = document_storage_key(target.owner_id, target.content_hash)
            job = IngestionJob(
                document_id=target.document_id,
                owner_id=target.owner_id,
                storage_key=target.storage_key,
                content_hash=target.content_hash,
                mime_type=target.mime_type,
            )
        except (ValueError, ValidationError):
            return None
        return job if derived == target.storage_key else None

    async def _is_processing(self, job: IngestionJob) -> bool:
        target = await self._documents.get_ingestion_target_for_user(
            job.document_id, job.owner_id
        )
        await self._session.rollback()
        return target is not None and target.status is DocumentStatus.PROCESSING

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
        self,
        job: IngestionJob,
        reason: IngestionReason,
        *,
        final_attempt: bool,
        release_to: DocumentStatus,
    ) -> IngestionResult:
        """Retry later — or, on the last attempt, fail for good.

        :param release_to: the status this stage started from. A parse goes
            back to `pending`, a chunking to `parsed`, so a retry repeats the
            stage that failed and not the one that succeeded.
        """
        if final_attempt:
            return await self._fail(job, reason)
        released = await self._transition(
            job, expected=DocumentStatus.PROCESSING, new=release_to
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

    async def _release_after_interruption(
        self, job: IngestionJob, release_to: DocumentStatus
    ) -> None:
        try:
            await self._transition(
                job, expected=DocumentStatus.PROCESSING, new=release_to
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
