"""The ARQ queue and worker against a real Redis and real PostgreSQL — M3/S3.2.

Nothing about ARQ is mocked. Jobs are written to Redis by `ArqIngestionQueue` —
in the end-to-end tests by the API's own upload route through the real
dependency provider — and consumed by an `arq.worker.Worker` running the real
task functions in burst mode, which processes what is queued and returns.

Marked `redis`: skipped locally without a Redis, required in CI
(`REQUIRE_REDIS=1`).
"""

import hashlib
import io
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import structlog
from arq import create_pool
from arq.worker import Worker, func
from sqlalchemy import text

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import UserRepository
from backend.db.session import get_sessionmaker
from backend.ingestion import worker as worker_module
from backend.ingestion.queue import (
    INGEST_DOCUMENT_TASK,
    ArqIngestionQueue,
    ingestion_job_id,
    redis_settings_from,
)
from backend.ingestion.worker import (
    ingest_document,
    reap_stale_processing,
    recover_pending_documents,
)
from backend.security.jwt_handler import JWTHandler
from backend.services.document_service import DocumentService
from backend.storage import LocalStorage
from shared.interfaces.storage import StorageError
from shared.models.ingestion import IngestionJob
from shared.models.principal import Principal
from tests.pdfs import make_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


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


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


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


@pytest.fixture(autouse=True)
def _logging_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The worker's startup configures logging for the whole process. Undo it."""
    saved = structlog.get_config()
    arq_logger = logging.getLogger("arq")
    monkeypatch.setattr(arq_logger, "propagate", arq_logger.propagate)
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are real; only the wait between them is removed."""
    monkeypatch.setattr(worker_module, "RETRY_DELAYS_SECONDS", (0,))


async def _run_worker(*, storage: Any = None, max_tries: int = 3) -> Worker:
    """A real ARQ worker over the real task function, run until the queue is empty."""

    async def startup(ctx: dict[str, Any]) -> None:
        await worker_module.startup(ctx)
        ctx["ingest_max_tries"] = max_tries
        if storage is not None:
            ctx["storage"] = storage

    worker = Worker(
        functions=[
            func(ingest_document, name=INGEST_DOCUMENT_TASK, max_tries=max_tries)
        ],
        on_startup=startup,
        redis_settings=redis_settings_from(get_settings(), conn_retries=1),
        burst=True,
        poll_delay=0.05,
        keep_result=60,
        handle_signals=False,
    )
    try:
        await worker.main()
    finally:
        await worker.close()
    return worker


async def _owner(session: Any) -> Principal:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Owner"
    )
    await session.commit()
    return Principal(user_id=user.id, email=user.email, role=user.role)


async def _status(document_id: str) -> tuple[str, str | None]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status, failure_reason FROM documents WHERE id = :id"),
                {"id": uuid.UUID(document_id)},
            )
        ).one()
    return row.status, row.failure_reason


async def _uploaded_job(
    session: Any, root: Path, data: bytes | None = None
) -> IngestionJob:
    owner = await _owner(session)
    recorder = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=recorder
    ).upload_and_process(
        filename="paper.pdf", stream=io.BytesIO(data or make_pdf()), principal=owner
    )
    return recorder.jobs[0]


async def _queued_job_ids() -> list[str | None]:
    redis = await create_pool(redis_settings_from(get_settings()))
    try:
        return [job.job_id for job in await redis.queued_jobs()]
    finally:
        await redis.aclose()


# =============================================================================
# The queue
# =============================================================================


class TestTheArqQueue:
    async def test_a_job_is_written_to_redis_under_its_document_id(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded_job(committing_session, storage_root)

        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)

        assert await _queued_job_ids() == [ingestion_job_id(job.document_id)]

    async def test_enqueueing_the_same_document_twice_queues_one_job(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded_job(committing_session, storage_root)
        queue = ArqIngestionQueue(redis_settings_from(get_settings()))

        await queue.enqueue(job)
        await queue.enqueue(job)

        assert len(await _queued_job_ids()) == 1

    async def test_the_payload_is_the_job_as_plain_values(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        """Validated back into an IngestionJob by the worker — never unpickled as one."""
        job = await _uploaded_job(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        redis = await create_pool(redis_settings_from(get_settings()))
        try:
            (queued,) = await redis.queued_jobs()
        finally:
            await redis.aclose()

        assert queued.function == INGEST_DOCUMENT_TASK
        assert queued.args == (job.model_dump(mode="json"),)
        assert set(queued.args[0]) == {
            "document_id",
            "owner_id",
            "storage_key",
            "content_hash",
            "mime_type",
        }


# =============================================================================
# API → Redis → worker
# =============================================================================


class TestUploadThroughTheWorker:
    async def test_an_upload_is_consumed_and_verified_by_a_real_worker(
        self, committing_session: Any, api_client: Any, storage_root: Path
    ) -> None:
        """No queue override: the API's real provider writes to Redis."""
        owner = await _owner(committing_session)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)
        response = await api_client.post(
            "/api/v1/documents/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("paper.pdf", make_pdf(), "application/pdf")},
        )
        assert response.status_code == 202
        document_id = response.json()["id"]
        assert await _queued_job_ids() == [ingestion_job_id(document_id)]

        worker = await _run_worker()

        assert (worker.jobs_complete, worker.jobs_failed) == (1, 0)
        assert await _status(document_id) == ("pending", None)
        assert await _queued_job_ids() == []

    async def test_a_tampered_upload_fails_in_the_worker(
        self, committing_session: Any, api_client: Any, storage_root: Path
    ) -> None:
        owner = await _owner(committing_session)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)
        data = make_pdf()
        document_id = (
            await api_client.post(
                "/api/v1/documents/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("paper.pdf", data, "application/pdf")},
            )
        ).json()["id"]
        blob = storage_root / owner.user_id / f"{hashlib.sha256(data).hexdigest()}.pdf"
        blob.write_bytes(b"%PDF-1.7 not what was uploaded")

        worker = await _run_worker()

        assert worker.jobs_retried == 0, "an integrity failure must not be retried"
        assert await _status(document_id) == ("failed", "content_hash_mismatch")


# =============================================================================
# The worker's own boundary: payloads, retries, exhaustion
# =============================================================================


class TestTheWorkerTask:
    @pytest.mark.parametrize(
        "malform",
        [
            lambda p: {**p, "owner_id": None},
            lambda p: {**p, "principal": {"role": "administrator"}},
            lambda p: {k: v for k, v in p.items() if k != "mime_type"},
            lambda p: [p],
        ],
        ids=["null-owner", "extra-field", "missing-field", "not-an-object"],
    )
    async def test_a_malformed_payload_is_rejected_without_retry(
        self, committing_session: Any, storage_root: Path, malform: Any
    ) -> None:
        """Not acted on at all: no claim, no read, no retry.

        `extra-field` is an otherwise perfect job for a real document. Only
        validation refuses it — every later check would pass — so this is what
        proves the payload is validated rather than trusted.
        """
        job = await _uploaded_job(committing_session, storage_root)
        redis = await create_pool(redis_settings_from(get_settings()))
        try:
            await redis.enqueue_job(
                INGEST_DOCUMENT_TASK, malform(job.model_dump(mode="json"))
            )
        finally:
            await redis.aclose()
        spy = FlakyStorage(LocalStorage(storage_root), failures=0)

        worker = await _run_worker(storage=spy)

        assert (worker.jobs_complete, worker.jobs_retried) == (1, 0)
        assert spy.gets == 0, "storage was read for a payload that is not a job"
        assert await _status(job.document_id) == ("pending", None)

    async def test_a_transient_storage_failure_is_retried_until_it_succeeds(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded_job(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        flaky = FlakyStorage(LocalStorage(storage_root), failures=2)

        worker = await _run_worker(storage=flaky, max_tries=3)

        assert worker.jobs_retried == 2
        assert flaky.gets == 3
        assert await _status(job.document_id) == ("pending", None)

    async def test_retries_end_in_a_terminal_failure_not_an_endless_loop(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        """ADR-0009 §5: never infinite retry. The last attempt records FAILED."""
        job = await _uploaded_job(committing_session, storage_root)
        await ArqIngestionQueue(redis_settings_from(get_settings())).enqueue(job)
        always_down = FlakyStorage(LocalStorage(storage_root), failures=10**6)

        worker = await _run_worker(storage=always_down, max_tries=3)

        assert always_down.gets == 3
        assert worker.jobs_retried == 2
        assert await _status(job.document_id) == ("failed", "storage_read_failure")

    async def test_the_reaper_cron_function_fails_stale_claims(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        job = await _uploaded_job(committing_session, storage_root)
        await committing_session.execute(
            text(
                "UPDATE documents SET status = 'processing',"
                " updated_at = now() - interval '2 hours' WHERE id = :id"
            ),
            {"id": uuid.UUID(job.document_id)},
        )
        await committing_session.commit()
        ctx: dict[str, Any] = {}
        await worker_module.startup(ctx)

        reaped = await reap_stale_processing(ctx)

        assert reaped == [job.document_id]
        assert await _status(job.document_id) == ("failed", "stale_processing")

    async def test_the_reaper_cron_spares_a_claim_within_the_stale_threshold(
        self, committing_session: Any, storage_root: Path
    ) -> None:
        """Older than the job timeout, younger than the threshold: left alone.

        The worker's startup is what hands the reaper its threshold. Handing it
        the job timeout instead would fail claims the settings say to keep.
        """
        settings = get_settings()
        between = (
            settings.INGEST_JOB_TIMEOUT_SECONDS
            + settings.INGEST_STALE_PROCESSING_SECONDS
        ) // 2
        assert (
            settings.INGEST_JOB_TIMEOUT_SECONDS
            < between
            < settings.INGEST_STALE_PROCESSING_SECONDS
        )
        job = await _uploaded_job(committing_session, storage_root)
        await committing_session.execute(
            text(
                "UPDATE documents SET status = 'processing',"
                " updated_at = now() - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": between, "id": uuid.UUID(job.document_id)},
        )
        await committing_session.commit()
        ctx: dict[str, Any] = {}
        await worker_module.startup(ctx)

        reaped = await reap_stale_processing(ctx)

        assert reaped == []
        assert await _status(job.document_id) == ("processing", None)


class TestTheWorkerEntrypoint:
    def test_worker_settings_are_wired_as_adr_0009_requires(self) -> None:
        from backend.ingestion.worker_settings import WorkerSettings

        settings = get_settings()
        (function,) = WorkerSettings.functions
        crons = {job.coroutine: job for job in WorkerSettings.cron_jobs}
        reaper = crons[reap_stale_processing]
        recovery = crons[recover_pending_documents]

        assert function.name == INGEST_DOCUMENT_TASK
        assert function.coroutine is ingest_document
        assert function.max_tries == settings.INGEST_MAX_TRIES
        assert function.timeout_s == settings.INGEST_JOB_TIMEOUT_SECONDS
        assert len(crons) == len(WorkerSettings.cron_jobs) == 2
        assert reaper.run_at_startup is True
        assert recovery.run_at_startup is True
        assert recovery.unique is True
        assert WorkerSettings.keep_result == 0
        assert (
            settings.INGEST_STALE_PROCESSING_SECONDS
            > settings.INGEST_JOB_TIMEOUT_SECONDS
        )
