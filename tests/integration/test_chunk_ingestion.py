"""Chunking inside ingestion, against real PostgreSQL — M3/S3.4.

What `IngestionService` does with a chunking result: when it may run (only from
`parsed`, only once), what it stores (chunks with the provenance ADR-0007 §1 and
ADR-0012 §5 require, owned through their document), what each failure becomes,
and what a failure mid-way leaves behind (nothing half-written).

The chunker is the real `SectionAwareChunker` except where a test needs to count
its calls, make it fail, or feed a result PostgreSQL will refuse; those replace
it through the same `DocumentChunker` protocol the service is written against.
The extractor is real too, so a document reaches `parsed` the way it will in
production.
"""

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import pytest
from sqlalchemy import event, text

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import (
    DocumentChunkRepository,
    DocumentRepository,
    UserRepository,
)
from backend.db.session import get_sessionmaker
from backend.security.jwt_handler import JWTHandler
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import (
    IngestionService,
    TransientIngestionError,
)
from backend.storage import LocalStorage
from document_processing.chunker import SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from document_processing.tokenization import RegexTokenizer
from shared.interfaces.storage import document_storage_key
from shared.models.chunking import Chunk, ChunkingResult
from shared.models.extraction import ExtractedPage
from shared.models.ingestion import IngestionJob, IngestionOutcome, IngestionReason
from shared.models.principal import Principal
from tests.pdfs import make_paper_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


# ----------------------------------------------------------------- doubles


class CountingChunker:
    """The real chunker, counting how often it is called and with what."""

    def __init__(self, inner: Any = None) -> None:
        self.inner = inner or SectionAwareChunker(
            RegexTokenizer(), chunk_size=120, chunk_overlap=20
        )
        self.calls = 0
        self.pages_seen: list[int] = []

    def chunk(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
        self.calls += 1
        self.pages_seen.append(len(pages))
        result: ChunkingResult = self.inner.chunk(pages)
        return result


class FixedChunker:
    """Returns a chosen result, or raises a chosen error, without chunking."""

    def __init__(self, result: ChunkingResult | BaseException) -> None:
        self.result = result
        self.calls = 0

    def chunk(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


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
def root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


async def _owner(session: Any) -> Principal:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Owner"
    )
    await session.commit()
    return Principal(user_id=user.id, email=user.email, role=user.role)


async def _upload(
    session: Any, root: Path, owner: Principal, data: bytes | None = None
) -> IngestionJob:
    queue = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=queue
    ).upload_and_process(
        filename="paper.pdf",
        stream=io.BytesIO(data or make_paper_pdf()),
        principal=owner,
    )
    return queue.jobs[0]


def _service(session: Any, root: Path, *, chunker: Any = None) -> IngestionService:
    return IngestionService(
        session,
        LocalStorage(root),
        extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=8),
        chunker=chunker
        or SectionAwareChunker(RegexTokenizer(), chunk_size=120, chunk_overlap=20),
        max_pages=get_settings().MAX_PDF_PAGES,
    )


async def _ingest(
    root: Path, job: IngestionJob, *, chunker: Any = None, final: bool = False
) -> Any:
    async with get_sessionmaker()() as session:
        return await _service(session, root, chunker=chunker).ingest(
            job, final_attempt=final
        )


async def _row(document_id: str) -> tuple[str, str | None, int]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, failure_reason, chunk_count FROM documents"
                    " WHERE id = :id"
                ),
                {"id": uuid.UUID(document_id)},
            )
        ).one()
    return row.status, row.failure_reason, row.chunk_count


async def _chunk_rows(document_id: str) -> list[Any]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT chunk_index, content, section, page_start, page_end,"
                    " token_count, strategy_version, tokenizer_id"
                    " FROM document_chunks WHERE document_id = :id"
                    " ORDER BY chunk_index"
                ),
                {"id": uuid.UUID(document_id)},
            )
        ).all()
    return list(rows)


async def _to_parsed(root: Path, job: IngestionJob) -> None:
    """Leave the document exactly `parsed`, through the service's own path.

    A delivery whose chunk stage fails transiently releases its claim back to
    `parsed` — which is also the state a worker that died between the two
    stages leaves behind. No private call, and nothing stored.
    """
    with pytest.raises(TransientIngestionError):
        await _ingest(
            root, job, chunker=FixedChunker(RuntimeError("stop after parsing"))
        )


def _fixed(*contents: str) -> ChunkingResult:
    return ChunkingResult(
        chunks=tuple(
            Chunk(
                chunk_index=index,
                content=content,
                section="1 Introduction",
                page_start=1,
                page_end=1,
                token_count=max(len(content.split()), 1),
            )
            for index, content in enumerate(contents)
        ),
        strategy_version="section-aware/v1",
        tokenizer_id="regex-word/v1",
    )


# =============================================================================
# The happy path
# =============================================================================


class TestAParsedDocumentIsChunked:
    async def test_it_ends_chunked_with_its_chunks_stored(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        status, reason, chunk_count = await _row(job.document_id)
        rows = await _chunk_rows(job.document_id)
        assert (status, reason) == ("chunked", None)
        assert chunk_count == len(rows) > 1

    async def test_every_chunk_carries_its_provenance(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job)

        rows = await _chunk_rows(job.document_id)
        assert [row.chunk_index for row in rows] == list(range(len(rows)))
        for row in rows:
            assert row.content.strip()
            assert 1 <= row.page_start <= row.page_end
            assert row.token_count > 0
            assert row.strategy_version == "section-aware/v1"
            assert row.tokenizer_id == "regex-word/v1"
        assert {row.section for row in rows} >= {"1 Introduction", "References"}

    async def test_the_sections_follow_the_paper(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job)

        sections = [row.section for row in await _chunk_rows(job.document_id)]
        # In order, each section's chunks together: no interleaving.
        assert sections == sorted(sections, key=lambda s: sections.index(s))

    async def test_a_chunk_spanning_a_page_break_records_both_pages(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job)

        rows = await _chunk_rows(job.document_id)
        assert any(row.page_end > row.page_start for row in rows)
        assert max(row.page_end for row in rows) >= 2

    async def test_the_chunks_are_readable_only_by_their_owner(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        stranger = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job)

        async with get_sessionmaker()() as session:
            repo = DocumentChunkRepository(session)
            mine = await repo.list_for_document(job.document_id, owner.user_id)
            theirs = await repo.list_for_document(job.document_id, stranger.user_id)

        assert len(mine) > 1 and theirs == []
        assert mine[0].section is not None
        assert mine[0].page_start >= 1

    async def test_a_soft_deleted_document_hides_its_chunks(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job)

        await DocumentRepository(committing_session).soft_delete_for_user(
            job.document_id, owner.user_id
        )
        await committing_session.commit()

        async with get_sessionmaker()() as session:
            found = await DocumentChunkRepository(session).list_for_document(
                job.document_id, owner.user_id
            )
        assert found == []

    async def test_the_api_shows_chunked_and_no_chunk_text(
        self, committing_session: Any, api_client: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)

        response = await api_client.get(
            f"/api/v1/documents/{job.document_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "chunked"
        assert "chunk" not in response.json()
        assert "Introduction" not in response.text

    async def test_the_same_document_twice_produces_identical_chunks(
        self, committing_session: Any, root: Path
    ) -> None:
        """Determinism where it counts: through the database, not just in memory."""
        owner = await _owner(committing_session)
        data = make_paper_pdf()
        first = await _upload(committing_session, root, owner, data)
        second_owner = await _owner(committing_session)
        second = await _upload(committing_session, root, second_owner, data)

        await _ingest(root, first)
        await _ingest(root, second)

        def shape(rows: list[Any]) -> list[Any]:
            return [
                (
                    r.chunk_index,
                    r.content,
                    r.section,
                    r.page_start,
                    r.page_end,
                    r.token_count,
                )
                for r in rows
            ]

        assert shape(await _chunk_rows(first.document_id)) == shape(
            await _chunk_rows(second.document_id)
        )


# =============================================================================
# Only from parsed, and only once
# =============================================================================


class TestChunkingHappensOnce:
    async def test_a_parsed_document_is_chunked_by_a_later_delivery(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        assert (await _row(job.document_id))[0] == "parsed"

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        assert (await _row(job.document_id))[0] == "chunked"

    async def test_a_chunked_document_is_not_chunked_again(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job)
        before = await _chunk_rows(job.document_id)
        chunker = CountingChunker()

        result = await _ingest(root, job, chunker=chunker)

        assert result.outcome is IngestionOutcome.SKIPPED_CHUNKED
        assert chunker.calls == 0
        assert await _chunk_rows(job.document_id) == before

    async def test_concurrent_deliveries_chunk_once_and_store_one_copy(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        chunker = CountingChunker()

        results = await asyncio.gather(
            *[_ingest(root, job, chunker=chunker) for _ in range(6)]
        )

        assert chunker.calls == 1
        assert [r.outcome for r in results].count(IngestionOutcome.CHUNKED) == 1
        rows = await _chunk_rows(job.document_id)
        assert [row.chunk_index for row in rows] == list(range(len(rows)))

    async def test_a_retry_after_a_transient_failure_stores_one_copy(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, chunker=FixedChunker(RuntimeError("a blip")))
        assert (await _row(job.document_id))[0] == "parsed", "released to its stage"

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        rows = await _chunk_rows(job.document_id)
        assert [row.chunk_index for row in rows] == list(range(len(rows)))

    @pytest.mark.parametrize("status", ["ready", "failed"], ids=["ready", "failed"])
    async def test_a_finished_document_is_left_alone(
        self, committing_session: Any, root: Path, status: str
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        reason = ", failure_reason = 'storage_missing'" if status == "failed" else ""
        await committing_session.execute(
            text(f"UPDATE documents SET status = :s{reason} WHERE id = :id"),
            {"s": status, "id": uuid.UUID(job.document_id)},
        )
        await committing_session.commit()
        chunker = CountingChunker()

        result = await _ingest(root, job, chunker=chunker)

        assert result.outcome is IngestionOutcome.SKIPPED_TERMINAL
        assert chunker.calls == 0
        assert await _chunk_rows(job.document_id) == []

    async def test_a_deleted_document_is_not_chunked(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        await DocumentRepository(committing_session).soft_delete_for_user(
            job.document_id, owner.user_id
        )
        await committing_session.commit()
        chunker = CountingChunker()

        result = await _ingest(root, job, chunker=chunker)

        assert result.outcome is IngestionOutcome.REJECTED
        assert chunker.calls == 0
        assert await _chunk_rows(job.document_id) == []


# =============================================================================
# Ownership
# =============================================================================


class TestOwnership:
    async def test_a_job_naming_another_owner_chunks_nothing(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        intruder = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        forged = job.model_copy(
            update={
                "owner_id": intruder.user_id,
                "storage_key": document_storage_key(intruder.user_id, job.content_hash),
            }
        )
        chunker = CountingChunker()

        result = await _ingest(root, forged, chunker=chunker, final=True)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.REJECTED,
            IngestionReason.OWNERSHIP_MISMATCH,
        )
        assert chunker.calls == 0
        assert await _chunk_rows(job.document_id) == []
        assert (await _row(job.document_id))[0] == "parsed"

    async def test_chunks_are_written_only_after_an_owner_scoped_lookup_in_sql(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        statements: list[str] = []

        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await _ingest(root, job)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        insert = next(
            i for i, s in enumerate(statements) if "INSERT INTO document_chunks" in s
        )
        lookup = statements[insert - 1]
        assert lookup.lstrip().startswith("SELECT documents.id")
        assert "documents.user_id = " in lookup
        assert "documents.deleted_at IS NULL" in lookup
        advance = [
            s for s in statements[:insert] if s.lstrip().startswith("UPDATE documents")
        ][-1]
        assert "documents.user_id = " in advance
        assert "documents.status = " in advance, "not a compare-and-set"

    async def test_the_pages_it_chunks_are_read_owner_scoped(
        self, committing_session: Any, root: Path
    ) -> None:
        """The chunker is handed this owner's pages, never a bare document id."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        chunker = CountingChunker()

        await _ingest(root, job, chunker=chunker)

        assert chunker.pages_seen == [2]


# =============================================================================
# Failures
# =============================================================================


class TestFailures:
    async def test_a_parsed_document_whose_pages_are_gone_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        await committing_session.execute(
            text("DELETE FROM document_pages WHERE document_id = :id"),
            {"id": uuid.UUID(job.document_id)},
        )
        await committing_session.commit()

        result = await _ingest(root, job)

        assert result.reason is IngestionReason.NO_EXTRACTED_PAGES
        assert (await _row(job.document_id))[:2] == ("failed", "no_extracted_pages")

    async def test_a_chunker_that_produces_nothing_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        empty = ChunkingResult(
            chunks=(), strategy_version="section-aware/v1", tokenizer_id="regex-word/v1"
        )

        result = await _ingest(root, job, chunker=FixedChunker(empty))

        assert result.reason is IngestionReason.NO_CHUNKS_PRODUCED
        assert (await _row(job.document_id))[:2] == ("failed", "no_chunks_produced")
        assert await _chunk_rows(job.document_id) == []

    async def test_a_chunker_that_raises_is_retried_then_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        broken = FixedChunker(RuntimeError("a defect with SECRET detail"))

        with pytest.raises(TransientIngestionError) as raised:
            await _ingest(root, job, chunker=broken)
        assert raised.value.reason is IngestionReason.CHUNKING_FAILURE
        assert (await _row(job.document_id))[0] == "parsed"

        result = await _ingest(root, job, chunker=broken, final=True)

        assert (await _row(job.document_id))[:2] == ("failed", "chunking_failure")
        assert result.reason is IngestionReason.CHUNKING_FAILURE
        assert "SECRET" not in str(await _row(job.document_id))

    async def test_a_write_that_fails_leaves_no_chunks_and_no_status(
        self, committing_session: Any, root: Path
    ) -> None:
        """A NUL is a chunk PostgreSQL refuses; any failed write behaves so."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)

        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, chunker=FixedChunker(_fixed("fine", "bad\x00")))

        assert (await _row(job.document_id))[0] == "parsed"
        assert await _chunk_rows(job.document_id) == []

    async def test_after_a_failed_write_the_document_chunks_next_time(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, chunker=FixedChunker(_fixed("fine", "bad\x00")))

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        assert (await _row(job.document_id))[0] == "chunked"
        assert await _chunk_rows(job.document_id)

    async def test_on_the_last_attempt_a_write_failure_fails_the_document(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)

        result = await _ingest(
            root, job, chunker=FixedChunker(_fixed("bad\x00")), final=True
        )

        assert result.reason is IngestionReason.CHUNKING_FAILURE
        assert (await _row(job.document_id))[:2] == ("failed", "chunking_failure")
        assert await _chunk_rows(job.document_id) == []

    async def test_a_claim_reaped_during_chunking_stores_nothing(
        self, committing_session: Any, root: Path
    ) -> None:
        """The reaper decided this delivery was dead. Its chunks must not land."""
        import threading

        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _to_parsed(root, job)
        started, release = threading.Event(), threading.Event()

        class Blocking(CountingChunker):
            def chunk(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
                started.set()
                release.wait(10)
                return super().chunk(pages)

        delivery = asyncio.ensure_future(_ingest(root, job, chunker=Blocking()))
        try:
            assert await asyncio.to_thread(started.wait, 10), "chunking never began"
            async with get_sessionmaker()() as other:
                await other.execute(
                    text(
                        "UPDATE documents SET updated_at = now() - interval '1 day'"
                        " WHERE id = :id"
                    ),
                    {"id": uuid.UUID(job.document_id)},
                )
                await other.commit()
                reaped = await _service(other, root).reap_stale_processing(
                    stale_after_seconds=60
                )
            assert reaped == [job.document_id]
        finally:
            release.set()
        result = await delivery

        assert result.outcome is IngestionOutcome.CLAIM_LOST
        assert (await _row(job.document_id))[:2] == ("failed", "stale_processing")
        assert await _chunk_rows(job.document_id) == []

    async def test_the_logs_carry_counts_never_the_text(
        self, committing_session: Any, root: Path
    ) -> None:
        from structlog.testing import capture_logs

        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        with capture_logs() as logs:
            await _ingest(root, job)

        (chunked,) = [e for e in logs if e["event"] == "ingestion.chunked"]
        assert chunked["chunk_count"] > 1
        assert chunked["strategy_version"] == "section-aware/v1"
        assert "Introduction" not in repr(logs)
        assert str(root) not in repr(logs)
