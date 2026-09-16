"""The ingestion service against real PostgreSQL and a real filesystem — M3/S3.2.

Every document here is created by S3.1's real upload path, and every job is the
one that upload actually built, captured by a recording queue. So what is under
test is what the worker will really receive.

Storage is the real `LocalStorage` under `tmp_path`, wrapped where a test needs
to watch it or break it. The wrappers implement the `Storage` protocol — the
same seam the service is written against — rather than patching internals.

ARQ is not involved; `test_ingestion_worker.py` runs the same service inside a
real ARQ worker against a real Redis.

Since M3/S3.3 a sound document is parsed rather than released to `pending`, so
the extractor here is the real one — PyMuPDF in a separate process — and the
success these tests expect is `parsed`. Parsing's own cases are in
`test_pdf_ingestion.py`; these keep S3.2's guarantees in force around it.
"""

import asyncio
import contextlib
import hashlib
import io
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import event, text, update
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from backend.api.app import app
from backend.api.dependencies.services import get_ingestion_queue
from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.models import DocumentORM
from backend.db.repositories import DocumentRepository, UserRepository
from backend.db.session import get_sessionmaker
from backend.ingestion.queue import ArqIngestionQueue
from backend.security.jwt_handler import JWTHandler
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import (
    IngestionService,
    TransientIngestionError,
)
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from document_processing.tokenization import RegexTokenizer, TOKENIZER_ID
from shared.interfaces.storage import StorageError, document_storage_key
from shared.models.document import DocumentStatus
from shared.models.ingestion import (
    IngestionJob,
    IngestionOutcome,
    IngestionReason,
)
from shared.models.principal import Principal
from shared.models.user import UserRole
from tests.pdfs import make_pdf
from tests.doubles import InMemoryVectorStore, StubEmbeddingProvider

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

P, S, R, F = (
    DocumentStatus.PENDING,
    DocumentStatus.PROCESSING,
    DocumentStatus.READY,
    DocumentStatus.FAILED,
)


# ----------------------------------------------------------- storage wrappers


class SpyStorage:
    """Counts reads and passes everything through."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.gets = 0

    async def put(self, key: str, data: bytes) -> bool:
        return bool(await self.inner.put(key, data))

    async def get(self, key: str) -> bytes:
        self.gets += 1
        return bytes(await self.inner.get(key))

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)


class BrokenStorage(SpyStorage):
    """Reads fail with a chosen exception."""

    def __init__(self, inner: Any, error: BaseException) -> None:
        super().__init__(inner)
        self.error = error

    async def get(self, key: str) -> bytes:
        self.gets += 1
        raise self.error


class DuringReadStorage(SpyStorage):
    """Runs a callback while a read is in flight, then reads."""

    def __init__(self, inner: Any, callback: Any) -> None:
        super().__init__(inner)
        self.callback = callback

    async def get(self, key: str) -> bytes:
        self.gets += 1
        await self.callback()
        return bytes(await self.inner.get(key))


class SlowStorage(SpyStorage):
    """Holds each read open briefly and records how many overlap."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.in_flight = 0
        self.most_in_flight = 0

    async def get(self, key: str) -> bytes:
        self.gets += 1
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.15)
            return bytes(await self.inner.get(key))
        finally:
            self.in_flight -= 1


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


# -------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _truncate_after_each_test() -> AsyncIterator[None]:
    yield
    async with get_engine().begin() as connection:
        await connection.execute(
            text("TRUNCATE document_chunks, documents, users RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


async def _owner(session: Any, role: UserRole = UserRole.RESEARCHER) -> Principal:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Owner", role=role
    )
    await session.commit()
    return Principal(user_id=user.id, email=user.email, role=user.role)


async def _upload(
    session: Any, storage: Any, owner: Principal, data: bytes | None = None
) -> IngestionJob:
    """A document created by the real upload service; returns the job it queued."""
    queue = RecordingQueue()
    await DocumentService(session, storage=storage, ingestion=queue).upload_and_process(
        filename="paper.pdf", stream=io.BytesIO(data or make_pdf()), principal=owner
    )
    assert len(queue.jobs) == 1
    return queue.jobs[0]


def _service(session: Any, storage: Any, *, vectors: Any = None) -> IngestionService:
    """The service as the worker builds it, with the real extractor."""
    return IngestionService(
        session,
        storage,
        extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=8),
        chunker=SectionAwareChunker(RegexTokenizer(), chunk_size=400, chunk_overlap=60),
        embedder=StubEmbeddingProvider(),
        vectors=vectors if vectors is not None else InMemoryVectorStore(),
        strategy_version=STRATEGY_VERSION,
        tokenizer_id=TOKENIZER_ID,
        max_pages=get_settings().MAX_PDF_PAGES,
    )


async def _ingest(
    storage: Any, job: IngestionJob, *, final: bool = False, vectors: Any = None
) -> Any:
    """One delivery, in its own session — as the worker runs it."""
    async with get_sessionmaker()() as session:
        return await _service(session, storage, vectors=vectors).ingest(
            job, final_attempt=final
        )


async def _row(session: Any, document_id: str) -> dict[str, Any]:
    result = await session.execute(
        text(
            "SELECT status, failure_reason, updated_at, deleted_at"
            " FROM documents WHERE id = :id"
        ),
        {"id": uuid.UUID(document_id)},
    )
    return dict(result.one()._mapping)


async def _set(session: Any, document_id: str, **values: Any) -> None:
    await session.execute(
        update(DocumentORM)
        .where(DocumentORM.id == uuid.UUID(document_id))
        .values(**values)
    )
    await session.commit()


async def _backdate(session: Any, document_id: str, seconds: int) -> None:
    await session.execute(
        text(
            "UPDATE documents SET updated_at = now() - make_interval(secs => :s)"
            " WHERE id = :id"
        ),
        {"s": seconds, "id": uuid.UUID(document_id)},
    )
    await session.commit()


def _blob(root: Path, job: IngestionJob) -> Path:
    return root / job.storage_key


# =============================================================================
# The happy path
# =============================================================================


class TestASoundDocumentIsVerified:
    async def test_it_is_verified_then_parsed(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """`parsed`, not `ready`: nothing has chunked it (ADR-0009, M3 DoD).

        S3.2 released a verified document back to `pending`, because no stage
        existed to take it further. S3.3's extraction is that stage.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        spy = SpyStorage(storage)

        result = await _ingest(spy, job)

        assert result.outcome is IngestionOutcome.READY
        assert result.document_id == job.document_id
        assert result.reason is None
        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == ("ready", None)
        assert spy.gets == 1

    async def test_the_document_is_processing_while_it_is_being_read(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """The claim covers the work. Observed from a separate session mid-read."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        seen: list[str] = []

        async def observe() -> None:
            async with get_sessionmaker()() as other:
                seen.append((await _row(other, job.document_id))["status"])

        await _ingest(DuringReadStorage(storage, observe), job)

        assert seen == ["processing"]

    async def test_ready_is_set_only_after_the_vectors_are_written(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """S3.4 asserted that nothing set `ready`. S3.5 is what sets it.

        The claim that replaces it is the one ADR-0013 §3 makes: `ready` means
        searchable, so it is committed **after** the vector store accepted the
        upsert and never before. Here the store refuses, and the document does
        not become `ready` however many times the job is delivered.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        vectors = InMemoryVectorStore()
        vectors.fail_upsert = True

        for _ in range(3):
            with contextlib.suppress(TransientIngestionError):
                await _ingest(storage, job, vectors=vectors)

        assert (await _row(committing_session, job.document_id))["status"] != "ready"
        assert vectors.points == {}

        vectors.fail_upsert = False
        assert (await _ingest(storage, job, vectors=vectors)).outcome is (
            IngestionOutcome.READY
        )
        assert (await _row(committing_session, job.document_id))["status"] == "ready"
        assert vectors.points


# =============================================================================
# The job is checked against the database before anything is touched
# =============================================================================


class TestAJobThatDisagreesWithTheDatabaseIsRejected:
    async def _assert_untouched(
        self, session: Any, job: IngestionJob, before: dict[str, Any], spy: SpyStorage
    ) -> None:
        after = await _row(session, job.document_id)
        assert after == before, "the document was modified"
        assert spy.gets == 0, "storage was read for a rejected job"

    async def test_a_job_naming_another_owner_is_rejected(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """A forged job for a real document, internally consistent.

        The owner is a real second user and the storage key is derived for them,
        so only the database can say the job is wrong. It does: the owner-scoped
        read returns nothing, and the document is never changed.
        """
        owner = await _owner(committing_session)
        intruder = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        forged = job.model_copy(
            update={
                "owner_id": intruder.user_id,
                "storage_key": document_storage_key(intruder.user_id, job.content_hash),
            }
        )
        before = await _row(committing_session, job.document_id)
        spy = SpyStorage(storage)

        result = await _ingest(spy, forged)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.REJECTED,
            IngestionReason.OWNERSHIP_MISMATCH,
        )
        await self._assert_untouched(committing_session, job, before, spy)

    async def test_even_a_final_attempt_does_not_touch_the_owners_document(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        intruder = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        forged = job.model_copy(
            update={
                "owner_id": intruder.user_id,
                "storage_key": document_storage_key(intruder.user_id, job.content_hash),
            }
        )

        await _ingest(storage, forged, final=True)

        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == ("pending", None)

    async def test_a_swapped_owner_with_the_original_key_is_rejected_before_any_read(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """The key must be what the owner and hash derive to."""
        owner = await _owner(committing_session)
        intruder = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        forged = job.model_copy(update={"owner_id": intruder.user_id})
        spy = SpyStorage(storage)

        result = await _ingest(spy, forged)

        assert result.reason is IngestionReason.STORAGE_KEY_MISMATCH
        assert spy.gets == 0

    async def test_a_job_with_another_contents_hash_is_rejected(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        other_hash = hashlib.sha256(b"something else").hexdigest()
        forged = job.model_copy(
            update={
                "content_hash": other_hash,
                "storage_key": document_storage_key(owner.user_id, other_hash),
            }
        )
        before = await _row(committing_session, job.document_id)
        spy = SpyStorage(storage)

        result = await _ingest(spy, forged)

        assert result.reason is IngestionReason.CONTENT_HASH_MISMATCH
        await self._assert_untouched(committing_session, job, before, spy)

    async def test_a_job_with_another_mime_type_is_rejected(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        before = await _row(committing_session, job.document_id)
        spy = SpyStorage(storage)

        result = await _ingest(spy, job.model_copy(update={"mime_type": "text/html"}))

        assert result.reason is IngestionReason.MIME_TYPE_MISMATCH
        await self._assert_untouched(committing_session, job, before, spy)

    async def test_a_job_with_a_malformed_owner_is_invalid(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        result = await _ingest(storage, job.model_copy(update={"owner_id": "../../x"}))

        assert result.reason is IngestionReason.INVALID_JOB

    async def test_a_document_that_does_not_exist_is_not_found(
        self, storage: LocalStorage
    ) -> None:
        owner_id = str(uuid.uuid4())
        content = hashlib.sha256(b"x").hexdigest()
        job = IngestionJob(
            document_id=str(uuid.uuid4()),
            owner_id=owner_id,
            storage_key=document_storage_key(owner_id, content),
            content_hash=content,
            mime_type="application/pdf",
        )
        spy = SpyStorage(storage)

        result = await _ingest(spy, job)

        assert result.reason is IngestionReason.DOCUMENT_NOT_FOUND
        assert spy.gets == 0

    async def test_a_deleted_document_is_not_found_and_stays_deleted(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        await DocumentRepository(committing_session).soft_delete_for_user(
            job.document_id, owner.user_id
        )
        await committing_session.commit()

        result = await _ingest(storage, job)

        assert result.reason is IngestionReason.DOCUMENT_NOT_FOUND
        row = await _row(committing_session, job.document_id)
        assert row["deleted_at"] is not None and row["status"] == "pending"


# =============================================================================
# Integrity of the stored bytes
# =============================================================================


class TestADocumentWhoseBytesAreWrongFails:
    async def test_a_modified_file_fails_permanently_with_the_reason(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        with _blob(tmp_path / "storage", job).open("ab") as blob:
            blob.write(b"one extra byte")

        result = await _ingest(storage, job)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.FAILED,
            IngestionReason.CONTENT_HASH_MISMATCH,
        )
        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == (
            "failed",
            "content_hash_mismatch",
        )

    async def test_integrity_failure_is_not_retried_even_with_attempts_left(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        """Permanent: no TransientIngestionError, on the first attempt."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        _blob(tmp_path / "storage", job).write_bytes(b"%PDF-1.7 replaced")

        result = await _ingest(storage, job, final=False)

        assert result.outcome is IngestionOutcome.FAILED

    async def test_a_missing_file_fails_permanently(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        _blob(tmp_path / "storage", job).unlink()

        result = await _ingest(storage, job)

        assert result.reason is IngestionReason.STORAGE_MISSING
        assert (await _row(committing_session, job.document_id))[
            "failure_reason"
        ] == "storage_missing"

    async def test_an_unsupported_mime_type_fails_without_reading(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        await _set(committing_session, job.document_id, mime_type="text/plain")
        spy = SpyStorage(storage)

        result = await _ingest(spy, job.model_copy(update={"mime_type": "text/plain"}))

        assert result.reason is IngestionReason.UNSUPPORTED_MIME_TYPE
        assert spy.gets == 0

    async def test_the_api_reports_failed_without_the_reason_or_any_path(
        self, committing_session: Any, api_client: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
        get_settings.cache_clear()
        storage = LocalStorage(tmp_path / "storage")
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        _blob(tmp_path / "storage", job).write_bytes(b"%PDF-1.7 replaced")
        await _ingest(storage, job)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)

        response = await api_client.get(
            f"/api/v1/documents/{job.document_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.json()["status"] == "failed"
        assert set(response.json()) == {"id", "filename", "status", "metadata"}
        assert str(tmp_path) not in response.text
        assert job.storage_key not in response.text


# =============================================================================
# Transient failures: retry, then give up honestly
# =============================================================================


class TestTransientFailures:
    async def test_a_storage_error_with_attempts_left_releases_and_retries(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        with pytest.raises(TransientIngestionError) as raised:
            await _ingest(BrokenStorage(storage, StorageError("unavailable")), job)

        assert raised.value.reason is IngestionReason.STORAGE_READ_FAILURE
        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == ("pending", None)

    async def test_a_storage_error_on_the_final_attempt_fails_for_good(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        result = await _ingest(
            BrokenStorage(storage, StorageError("unavailable")), job, final=True
        )

        assert result.reason is IngestionReason.STORAGE_READ_FAILURE
        assert (await _row(committing_session, job.document_id))["status"] == "failed"

    async def test_an_unexpected_error_is_retried_then_fails_as_processing_failure(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        broken = BrokenStorage(storage, RuntimeError("something nobody expected"))

        with pytest.raises(TransientIngestionError):
            await _ingest(broken, job)
        assert (await _row(committing_session, job.document_id))["status"] == "pending"

        result = await _ingest(broken, job, final=True)

        assert result.reason is IngestionReason.PROCESSING_FAILURE
        row = await _row(committing_session, job.document_id)
        assert row["failure_reason"] == "processing_failure"
        assert "nobody expected" not in str(row)

    async def test_a_retry_after_a_transient_failure_succeeds(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        with pytest.raises(TransientIngestionError):
            await _ingest(BrokenStorage(storage, StorageError("blip")), job)

        result = await _ingest(storage, job)

        assert result.outcome is IngestionOutcome.READY


# =============================================================================
# State and idempotency
# =============================================================================


class TestDeliveryIsIdempotent:
    @pytest.mark.parametrize(
        ("status", "reason", "outcome"),
        [
            ("processing", None, IngestionOutcome.SKIPPED_IN_PROGRESS),
            ("ready", None, IngestionOutcome.SKIPPED_TERMINAL),
            ("failed", "storage_missing", IngestionOutcome.SKIPPED_TERMINAL),
        ],
        ids=["processing", "ready", "failed"],
    )
    async def test_a_document_not_pending_is_left_exactly_as_it_is(
        self,
        committing_session: Any,
        storage: LocalStorage,
        status: str,
        reason: str | None,
        outcome: IngestionOutcome,
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        await _set(
            committing_session,
            job.document_id,
            status=DocumentStatus(status),
            failure_reason=reason,
        )
        before = await _row(committing_session, job.document_id)
        spy = SpyStorage(storage)

        result = await _ingest(spy, job)

        assert result.outcome is outcome
        assert await _row(committing_session, job.document_id) == before
        assert spy.gets == 0

    async def test_a_chunked_document_is_embedded_rather_than_skipped(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """Since M3/S3.5 `chunked` is a stage's starting point, not the end.

        Before it, a redelivered `chunked` document was skipped and nothing
        ever set `ready`. It is the change that completes the pipeline, so it
        is asserted where the old behaviour was.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        await _ingest(storage, job)
        # Back to where a delivery interrupted after chunking would leave it.
        await _set(committing_session, job.document_id, status=DocumentStatus.CHUNKED)
        spy = SpyStorage(storage)

        result = await _ingest(spy, job)

        assert result.outcome is IngestionOutcome.READY
        assert (await _row(committing_session, job.document_id))["status"] == "ready"
        # Embedding reads chunks, never the stored PDF.
        assert spy.gets == 0

    async def test_the_same_job_twice_is_harmless(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        first = await _ingest(storage, job)
        second = await _ingest(storage, job)

        assert (first.outcome, second.outcome) == (
            IngestionOutcome.READY,
            IngestionOutcome.SKIPPED_TERMINAL,
        )
        assert (await _row(committing_session, job.document_id))["status"] == "ready"

    async def test_a_failed_document_redelivered_keeps_its_reason(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        _blob(tmp_path / "storage", job).unlink()
        await _ingest(storage, job)
        _blob(tmp_path / "storage", job).write_bytes(b"restored? irrelevant")

        result = await _ingest(storage, job)

        assert result.outcome is IngestionOutcome.SKIPPED_TERMINAL
        assert (await _row(committing_session, job.document_id))[
            "failure_reason"
        ] == "storage_missing"

    async def test_concurrent_deliveries_never_work_on_a_document_at_once(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """Six deliveries at once. The claim is a compare-and-set in PostgreSQL.

        Reads are held open, so if two deliveries both held the claim their reads
        would overlap. They never do. Every delivery ends without an error, and
        the document ends `ready` — by exactly one of them.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        slow = SlowStorage(storage)

        results = await asyncio.gather(*[_ingest(slow, job) for _ in range(6)])

        assert slow.most_in_flight == 1
        outcomes = [r.outcome for r in results]
        assert outcomes.count(IngestionOutcome.READY) == 1
        assert set(outcomes) <= {
            IngestionOutcome.READY,
            IngestionOutcome.SKIPPED_IN_PROGRESS,
            IngestionOutcome.SKIPPED_TERMINAL,
        }
        assert (await _row(committing_session, job.document_id))["status"] == "ready"

    async def test_an_interrupted_delivery_puts_the_document_back(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """Cancelled mid-read — a job timeout, or a worker stopping."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        started = asyncio.Event()

        async def hang() -> None:
            started.set()
            await asyncio.sleep(30)

        task = asyncio.ensure_future(_ingest(DuringReadStorage(storage, hang), job))
        try:
            # Bounded: if the delivery never reaches storage, fail, not hang.
            await asyncio.wait_for(started.wait(), timeout=10)
        except TimeoutError:
            task.cancel()
            raise
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)

        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == ("pending", None)
        assert (await _ingest(storage, job)).outcome is IngestionOutcome.READY

    async def test_a_claim_taken_by_the_reaper_is_not_overwritten(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        """The worker is slow; the reaper decides it is gone and fails the document.

        When the worker finishes, its release is a compare-and-set on
        `processing`, finds `failed`, and changes nothing.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        async def reaper_intervenes() -> None:
            async with get_sessionmaker()() as other:
                await _backdate(other, job.document_id, 10_000)
                await _service(other, storage).reap_stale_processing(
                    stale_after_seconds=60
                )

        result = await _ingest(DuringReadStorage(storage, reaper_intervenes), job)

        assert result.outcome is IngestionOutcome.CLAIM_LOST
        row = await _row(committing_session, job.document_id)
        assert (row["status"], row["failure_reason"]) == (
            "failed",
            "stale_processing",
        )


# =============================================================================
# Failures are persisted, and safe
# =============================================================================


class TestFailuresArePersistedSafely:
    async def test_every_failure_reason_is_a_plain_code(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        owner = await _owner(committing_session)
        tampered = await _upload(committing_session, storage, owner, make_pdf(pages=1))
        missing = await _upload(committing_session, storage, owner, make_pdf(pages=2))
        _blob(tmp_path / "storage", tampered).write_bytes(b"%PDF-1.7 no")
        _blob(tmp_path / "storage", missing).unlink()

        await _ingest(storage, tampered)
        await _ingest(storage, missing)

        reasons = (
            (
                await committing_session.execute(
                    text("SELECT failure_reason FROM documents WHERE status = 'failed'")
                )
            )
            .scalars()
            .all()
        )
        assert sorted(reasons) == ["content_hash_mismatch", "storage_missing"]
        for reason in reasons:
            assert re.fullmatch(r"[a-z_]+", reason), reason
            assert str(tmp_path) not in reason

    async def test_the_database_refuses_a_failure_without_a_reason(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)

        with pytest.raises(IntegrityError):
            await _set(committing_session, job.document_id, status=F)
        await committing_session.rollback()

        with pytest.raises(IntegrityError):
            await _set(committing_session, job.document_id, failure_reason="x")

    async def test_ingestion_logs_carry_no_path_filename_or_content(
        self, committing_session: Any, storage: LocalStorage, tmp_path: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(
            committing_session, storage, owner, make_pdf(text="SECRET-MANUSCRIPT")
        )
        broken = await _upload(committing_session, storage, owner, make_pdf(pages=3))
        _blob(tmp_path / "storage", broken).unlink()

        with capture_logs() as logs:
            await _ingest(storage, job)
            await _ingest(storage, broken)

        events = {e["event"] for e in logs}
        assert {"ingestion.claimed", "ingestion.parsed", "ingestion.failed"} <= events
        everything = repr(logs)
        for secret in (str(tmp_path), "paper.pdf", "SECRET-MANUSCRIPT", "%PDF"):
            assert secret not in everything, secret


# =============================================================================
# The stale-job reaper
# =============================================================================


class TestTheReaper:
    async def _reap(self, storage: Any, seconds: int = 900) -> list[str]:
        async with get_sessionmaker()() as session:
            return await _service(session, storage).reap_stale_processing(
                stale_after_seconds=seconds
            )

    async def test_it_fails_only_stale_processing_documents(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        other_owner = await _owner(committing_session)
        docs = {
            name: (await _upload(committing_session, storage, who, make_pdf(pages=n)))
            for n, (name, who) in enumerate(
                [
                    ("stale", owner),
                    ("stale_other_owner", other_owner),
                    ("fresh", owner),
                    ("old_pending", owner),
                    ("old_ready", owner),
                    ("old_failed", owner),
                    ("stale_deleted", owner),
                ],
                start=1,
            )
        }
        for name in ("stale", "stale_other_owner", "fresh", "stale_deleted"):
            await _set(committing_session, docs[name].document_id, status=S)
        await _set(committing_session, docs["old_ready"].document_id, status=R)
        await _set(
            committing_session,
            docs["old_failed"].document_id,
            status=F,
            failure_reason="storage_missing",
        )
        await DocumentRepository(committing_session).soft_delete_for_user(
            docs["stale_deleted"].document_id, owner.user_id
        )
        await committing_session.commit()
        for name in docs:
            if name != "fresh":
                await _backdate(committing_session, docs[name].document_id, 5_000)

        reaped = await self._reap(storage)

        assert sorted(reaped) == sorted(
            [docs["stale"].document_id, docs["stale_other_owner"].document_id]
        )
        expected = {
            "stale": ("failed", "stale_processing"),
            "stale_other_owner": ("failed", "stale_processing"),
            "fresh": ("processing", None),
            "old_pending": ("pending", None),
            "old_ready": ("ready", None),
            "old_failed": ("failed", "storage_missing"),
            "stale_deleted": ("processing", None),
        }
        for name, (status, reason) in expected.items():
            row = await _row(committing_session, docs[name].document_id)
            assert (row["status"], row["failure_reason"]) == (status, reason), name

    async def test_a_claim_younger_than_the_threshold_is_left_alone(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        """Just under the line stays; just over it goes."""
        owner = await _owner(committing_session)
        under = await _upload(committing_session, storage, owner, make_pdf(pages=1))
        over = await _upload(committing_session, storage, owner, make_pdf(pages=2))
        for job in (under, over):
            await _set(committing_session, job.document_id, status=S)
        await _backdate(committing_session, under.document_id, 890)
        await _backdate(committing_session, over.document_id, 910)

        reaped = await self._reap(storage, seconds=900)

        assert reaped == [over.document_id]
        assert (await _row(committing_session, under.document_id))["status"] == (
            "processing"
        )

    async def test_a_reaped_document_is_terminal(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        await _set(committing_session, job.document_id, status=S)
        await _backdate(committing_session, job.document_id, 5_000)
        await self._reap(storage)

        assert (
            await _ingest(storage, job)
        ).outcome is IngestionOutcome.SKIPPED_TERMINAL


# =============================================================================
# The upload side: committed before enqueue, and undone if the queue refuses
# =============================================================================


class TestAnUploadThatCannotBeQueuedIsUndone:
    async def _account(self, session: Any) -> tuple[Principal, dict[str, str]]:
        owner = await _owner(session)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)
        return owner, {"Authorization": f"Bearer {token}"}

    def _dead_redis(self) -> None:
        """The real ARQ queue, pointed at a port nothing listens on."""
        from arq.connections import RedisSettings

        app.dependency_overrides[get_ingestion_queue] = lambda: ArqIngestionQueue(
            RedisSettings(host="127.0.0.1", port=1, conn_timeout=1, conn_retries=0)
        )

    async def test_it_is_503_and_nothing_live_is_left(
        self,
        committing_session: Any,
        api_client: Any,
        tmp_path: Path,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
        get_settings.cache_clear()
        _, headers = await self._account(committing_session)
        self._dead_redis()

        response = await api_client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("paper.pdf", make_pdf(), "application/pdf")},
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Document processing is unavailable. The upload was not kept."
        }
        assert (await api_client.get("/api/v1/documents/", headers=headers)).json()[
            "total"
        ] == 0
        rows = (
            (await committing_session.execute(text("SELECT deleted_at FROM documents")))
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0] is not None, "the row is soft-deleted"
        assert not [p for p in (tmp_path / "storage").rglob("*.pdf")], "blob kept"

    async def test_retrying_once_the_queue_is_back_succeeds(
        self,
        committing_session: Any,
        api_client: Any,
        tmp_path: Path,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
        get_settings.cache_clear()
        _, headers = await self._account(committing_session)
        data = make_pdf()
        self._dead_redis()
        assert (
            await api_client.post(
                "/api/v1/documents/upload",
                headers=headers,
                files={"file": ("paper.pdf", data, "application/pdf")},
            )
        ).status_code == 503

        recorder = RecordingQueue()
        app.dependency_overrides[get_ingestion_queue] = lambda: recorder
        again = await api_client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("paper.pdf", data, "application/pdf")},
        )

        assert again.status_code == 202
        assert [job.document_id for job in recorder.jobs] == [again.json()["id"]]

    async def test_a_file_that_already_belonged_to_another_row_is_kept(
        self,
        committing_session: Any,
        api_client: Any,
        tmp_path: Path,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
        get_settings.cache_clear()
        _, headers = await self._account(committing_session)
        data = make_pdf()
        first = await api_client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("paper.pdf", data, "application/pdf")},
        )
        await api_client.delete(
            f"/api/v1/documents/{first.json()['id']}", headers=headers
        )
        self._dead_redis()

        response = await api_client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("paper.pdf", data, "application/pdf")},
        )

        assert response.status_code == 503
        assert len(list((tmp_path / "storage").rglob("*.pdf"))) == 1


# =============================================================================
# Ownership stays in the SQL
# =============================================================================


class TestTheWorkersQueriesCarryTheOwner:
    async def test_read_and_transition_filter_by_owner_in_sql(
        self, committing_session: Any, storage: LocalStorage
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, storage, owner)
        statements: list[str] = []

        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await _ingest(storage, job)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        reads = [
            s
            for s in statements
            if s.lstrip().startswith("SELECT")
            and "FROM documents" in s
            and "storage_key" in s
        ]
        writes = [s for s in statements if s.lstrip().startswith("UPDATE documents")]
        # Three stages, three claims and three advances: pending -> processing
        # -> parsed -> processing -> chunked -> processing -> ready (M3/S3.5).
        assert reads and len(writes) == 6, (reads, writes)
        for statement in reads + writes:
            assert "documents.user_id = " in statement, statement
            assert "documents.deleted_at IS NULL" in statement, statement
        for statement in writes:
            assert "documents.status = " in statement, "not a compare-and-set"
