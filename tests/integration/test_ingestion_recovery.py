"""Recovering `pending` documents no job will pick up — M3/S3.3.

S3.2 left a gap: a document could end `pending` with no job anywhere — an ARQ job
timeout releases the claim and is not retried; a Redis outage can fall between
an upload's commit and its enqueue — and nothing would ever look at it again.
The recovery sweep closes it. These tests run it against a real PostgreSQL, a
real Redis and, where a job has to be genuinely active, a real ARQ worker.

Marked `db` and `redis`: skipped locally without them, required in CI.
"""

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from arq import create_pool
from arq.cron import cron
from arq.worker import Worker, func
from sqlalchemy import text

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import DocumentRepository, UserRepository
from backend.db.session import get_sessionmaker
from backend.ingestion import worker as worker_module
from backend.ingestion.queue import (
    INGEST_DOCUMENT_TASK,
    ArqIngestionQueue,
    PooledArqIngestionQueue,
    ingestion_job_id,
    redis_settings_from,
)
from backend.ingestion.worker import ingest_document, recover_pending_documents
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import IngestionService
from backend.storage import LocalStorage
from document_processing.pdf_parser import PyMuPDFTextExtractor
from shared.interfaces.storage import StorageError, document_storage_key
from shared.models.document import DocumentType
from shared.models.ingestion import IngestionJob, RecoveryResult
from shared.models.principal import Principal
from tests.pdfs import make_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


class RecordingQueue:
    """Stands in for Redis at upload time, so the document starts with no job."""

    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


class FlakyStorage:
    """Fails the first `failures` reads with a storage error, then reads."""

    def __init__(self, inner: Any, failures: int) -> None:
        self.inner = inner
        self.failures = failures
        self.gets = 0

    async def put(self, key: str, data: bytes) -> bool:
        return bool(await self.inner.put(key, data))

    async def get(self, key: str) -> bytes:
        self.gets += 1
        if self.gets <= self.failures:
            raise StorageError("storage briefly unavailable")
        return bytes(await self.inner.get(key))

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)


# -------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _clean_redis_and_tables() -> AsyncIterator[None]:
    redis = await create_pool(redis_settings_from(get_settings()))
    await redis.flushdb()
    try:
        yield
    finally:
        await redis.flushdb()
        await redis.aclose()
        async with get_engine().begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE document_chunks, documents, users RESTART IDENTITY CASCADE"
                )
            )


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    get_settings.cache_clear()
    return root


async def _owner(session: Any) -> Principal:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Owner"
    )
    await session.commit()
    return Principal(user_id=user.id, email=user.email, role=user.role)


async def _uploaded(
    session: Any, root: Path, owner: Principal | None = None
) -> IngestionJob:
    """A real upload whose job never reached Redis."""
    owner = owner or await _owner(session)
    recorder = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=recorder
    ).upload_and_process(
        filename="paper.pdf", stream=io.BytesIO(make_pdf()), principal=owner
    )
    return recorder.jobs[0]


async def _sql(session: Any, statement: str, **params: Any) -> None:
    await session.execute(text(statement), params)
    await session.commit()


def _service(session: Any, root: Path) -> IngestionService:
    return IngestionService(
        session,
        LocalStorage(root),
        extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=4),
        max_pages=get_settings().MAX_PDF_PAGES,
    )


async def _sweep(root: Path, *, limit: int = 500) -> RecoveryResult:
    redis = await create_pool(redis_settings_from(get_settings()))
    try:
        async with get_sessionmaker()() as session:
            return await _service(session, root).recover_pending(
                PooledArqIngestionQueue(redis), limit=limit
            )
    finally:
        await redis.aclose()


async def _queued() -> dict[str, Any]:
    """Job id → payload, for every job in the queue."""
    redis = await create_pool(redis_settings_from(get_settings()))
    try:
        return {str(job.job_id): job.args[0] for job in await redis.queued_jobs()}
    finally:
        await redis.aclose()


async def _status(document_id: str) -> tuple[str, str | None]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status, failure_reason FROM documents WHERE id = :id"),
                {"id": uuid.UUID(document_id)},
            )
        ).one()
    return row.status, row.failure_reason


# =============================================================================
# What is recovered
# =============================================================================


class TestAPendingDocumentWithoutAJobIsRequeued:
    async def test_it_gets_exactly_one_job(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded(committing_session, storage_root)
        assert await _queued() == {}

        result = await _sweep(storage_root)

        assert result.requeued == (job.document_id,)
        assert (result.already_queued, result.skipped) == (0, 0)
        assert list(await _queued()) == [ingestion_job_id(job.document_id)]

    async def test_the_job_is_built_from_the_persisted_row(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        """Owner, key, hash and type are the row's — there is no request here.

        Byte-for-byte the job the upload itself built, which is the evidence
        that nothing but the database went into it.
        """
        job = await _uploaded(committing_session, storage_root)

        await _sweep(storage_root)

        payload = (await _queued())[ingestion_job_id(job.document_id)]
        assert payload == job.model_dump(mode="json")
        assert payload["storage_key"] == document_storage_key(
            payload["owner_id"], payload["content_hash"]
        )

    async def test_a_second_sweep_adds_nothing(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded(committing_session, storage_root)
        await _sweep(storage_root)

        again = await _sweep(storage_root)

        assert again == RecoveryResult(requeued=(), already_queued=1)
        assert list(await _queued()) == [ingestion_job_id(job.document_id)]

    async def test_the_oldest_are_taken_first_and_the_limit_holds(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        owner = await _owner(committing_session)
        jobs = [
            await _uploaded(committing_session, storage_root, owner) for _ in range(3)
        ]
        for age, job in zip((100, 300, 200), jobs):
            await _sql(
                committing_session,
                "UPDATE documents SET updated_at = now() - make_interval(secs => :s)"
                " WHERE id = :id",
                s=age,
                id=uuid.UUID(job.document_id),
            )

        result = await _sweep(storage_root, limit=2)

        assert result.requeued == (jobs[1].document_id, jobs[2].document_id)


class TestADocumentWithAJobGetsNoSecond:
    async def test_a_queued_job_is_not_duplicated(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)

        result = await _sweep(storage_root)

        assert result == RecoveryResult(requeued=(), already_queued=1)
        assert len(await _queued()) == 1

    async def test_a_job_waiting_to_retry_is_not_duplicated(
        self,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An active job, not a staged one: a real worker, a real deferred retry.

        After a transient failure the delivery releases its claim, so the
        document is `pending` again while ARQ holds the job for its retry. That
        is precisely the state a careless sweep would duplicate.
        """
        monkeypatch.setattr(worker_module, "RETRY_DELAYS_SECONDS", (2,))
        job = await _uploaded(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        flaky = FlakyStorage(LocalStorage(storage_root), failures=1)
        worker = _worker(storage=flaky, max_tries=3)
        running = asyncio.ensure_future(worker.main())
        try:
            # Wait for the first attempt to fail and release its claim.
            async with asyncio.timeout(10):
                while not (
                    flaky.gets == 1 and (await _status(job.document_id))[0] == "pending"
                ):
                    await asyncio.sleep(0.05)
            result = await _sweep(storage_root)
            assert result == RecoveryResult(requeued=(), already_queued=1)
            assert list(await _queued()) == [ingestion_job_id(job.document_id)]
            async with asyncio.timeout(20):
                await running
        finally:
            running.cancel()
            await worker.close()

        assert worker.jobs_retried == 1
        assert flaky.gets == 2, "the document was read by exactly the one job"


class TestWhatIsNotRecovered:
    async def test_only_live_pending_documents_with_stored_content(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        owner = await _owner(committing_session)
        names = ["pending", "deleted", "failed", "ready", "processing"]
        jobs = {
            name: await _uploaded(committing_session, storage_root, owner)
            for name in names
        }
        await DocumentRepository(committing_session).soft_delete_for_user(
            jobs["deleted"].document_id, owner.user_id
        )
        await committing_session.commit()
        for name, extra in (
            ("failed", ", failure_reason = 'storage_missing'"),
            ("ready", ""),
            ("processing", ""),
        ):
            await _sql(
                committing_session,
                f"UPDATE documents SET status = :status{extra} WHERE id = :id",
                status=name,
                id=uuid.UUID(jobs[name].document_id),
            )
        # A document from before upload existed: pending, but nothing stored.
        legacy = await DocumentRepository(committing_session).create(
            user_id=owner.user_id, filename="legacy.pdf", doc_type=DocumentType.PDF
        )
        await committing_session.commit()

        result = await _sweep(storage_root)

        assert result.requeued == (jobs["pending"].document_id,)
        assert set(await _queued()) == {ingestion_job_id(jobs["pending"].document_id)}
        assert legacy.id not in result.requeued

    async def test_a_row_that_cannot_form_a_valid_job_is_reported_not_queued(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        """A key that is not what the row's owner and hash derive to.

        Only a hand-edited row gets here. A job built from it would be rejected
        on delivery, leaving it `pending` for the next sweep to queue again —
        every minute, forever. It is skipped instead, and left untouched.
        """
        job = await _uploaded(committing_session, storage_root)
        other = await _owner(committing_session)
        await _sql(
            committing_session,
            "UPDATE documents SET storage_key = :key WHERE id = :id",
            key=document_storage_key(other.user_id, job.content_hash),
            id=uuid.UUID(job.document_id),
        )

        result = await _sweep(storage_root)

        assert result == RecoveryResult(requeued=(), skipped=1)
        assert await _queued() == {}
        assert await _status(job.document_id) == ("pending", None)


class TestConcurrentSweeps:
    async def test_racing_sweeps_queue_one_job_per_document(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        owner = await _owner(committing_session)
        jobs = [
            await _uploaded(committing_session, storage_root, owner) for _ in range(5)
        ]

        results = await asyncio.gather(*[_sweep(storage_root) for _ in range(4)])

        requeued = [doc for result in results for doc in result.requeued]
        assert sorted(requeued) == sorted(job.document_id for job in jobs)
        assert sum(result.already_queued for result in results) == 5 * 3
        assert set(await _queued()) == {ingestion_job_id(j.document_id) for j in jobs}


# =============================================================================
# The worker: the cron, and a final attempt that gives up
# =============================================================================


def _worker(
    *, storage: Any = None, max_tries: int = 3, with_recovery: bool = False
) -> Worker:
    async def startup(ctx: dict[str, Any]) -> None:
        await worker_module.startup(ctx)
        ctx["ingest_max_tries"] = max_tries
        if storage is not None:
            ctx["storage"] = storage

    cron_jobs = (
        [
            cron(
                recover_pending_documents,
                minute=set(range(60)),
                second=30,
                run_at_startup=True,
            )
        ]
        if with_recovery
        else []
    )
    return Worker(
        functions=[
            func(ingest_document, name=INGEST_DOCUMENT_TASK, max_tries=max_tries)
        ],
        cron_jobs=cron_jobs,
        on_startup=startup,
        redis_settings=redis_settings_from(get_settings(), conn_retries=1),
        burst=True,
        poll_delay=0.05,
        # As WorkerSettings: no result is kept, so a finished job's id is free
        # for the sweep to use again. A kept result would block it.
        keep_result=0,
        handle_signals=False,
    )


@pytest.fixture(autouse=True)
def _logging_restored(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The worker's startup configures logging for the whole process. Undo it."""
    import logging

    import structlog

    saved = structlog.get_config()
    arq_logger = logging.getLogger("arq")
    monkeypatch.setattr(arq_logger, "propagate", arq_logger.propagate)
    yield
    structlog.configure(**saved)


class TestTheWorker:
    async def test_the_recovery_cron_hands_a_stranded_document_to_a_worker(
        self,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Upload, no job; the worker starts, the cron queues it, the worker runs it."""
        monkeypatch.setattr(worker_module, "RETRY_DELAYS_SECONDS", (0,))
        job = await _uploaded(committing_session, storage_root)
        storage = FlakyStorage(LocalStorage(storage_root), failures=0)
        worker = _worker(storage=storage, with_recovery=True)
        try:
            await worker.main()
        finally:
            await worker.close()

        assert storage.gets == 1, "the recovered job ran exactly once"
        assert worker.jobs_complete >= 2  # the cron pass, and the recovered job
        assert await _status(job.document_id) == ("parsed", None)

    async def test_a_final_attempt_that_gives_up_records_the_failure(
        self,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An error nothing classified, on every attempt, before any claim.

        Left `pending`, the sweep would give the document a fresh job and a
        fresh retry budget, forever. So the last attempt fails it — and a sweep
        afterwards finds nothing to queue.
        """
        monkeypatch.setattr(worker_module, "RETRY_DELAYS_SECONDS", (0,))

        def unexpected(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("a defect nothing anticipated")

        monkeypatch.setattr(IngestionService, "_job_disagrees_with", unexpected)
        job = await _uploaded(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        worker = _worker(max_tries=2)
        try:
            await worker.main()
        finally:
            await worker.close()

        assert (worker.jobs_retried, worker.jobs_failed) == (1, 1)
        assert await _status(job.document_id) == ("failed", "processing_failure")
        assert (await _sweep(storage_root)).requeued == ()

    async def test_when_the_failure_cannot_be_recorded_the_sweep_recovers_it(
        self,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The database is still down at the end: nothing to write the failure to.

        The document stays `pending`, and once the database answers again the
        sweep queues it — recovery, not a silent loss.
        """
        monkeypatch.setattr(worker_module, "RETRY_DELAYS_SECONDS", (0,))

        def unexpected(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

        async def cannot_record(*args: Any, **kwargs: Any) -> bool:
            raise ConnectionError("database unavailable")

        monkeypatch.setattr(IngestionService, "_job_disagrees_with", unexpected)
        monkeypatch.setattr(IngestionService, "record_given_up", cannot_record)
        job = await _uploaded(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        worker = _worker(max_tries=1)
        try:
            await worker.main()
        finally:
            await worker.close()

        assert worker.jobs_failed == 1
        assert await _status(job.document_id) == ("pending", None)
        assert (await _sweep(storage_root)).requeued == (job.document_id,)
