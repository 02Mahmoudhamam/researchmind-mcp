"""Parsing inside ingestion, against real PostgreSQL and a real filesystem — M3/S3.3.

What `IngestionService` does with extraction: when it may happen (only after the
bytes are verified), what it stores (every page, owned, once), what each failure
becomes (a persisted reason, retried only when the parser rather than the PDF
failed), and what a failure mid-way leaves behind (nothing half-done).

The extractor is the real `PyMuPDFTextExtractor` — a separate process with a
deadline — except where a test needs to count its calls, stall it or feed a
result PostgreSQL will refuse; those wrap or replace it through the same
`PdfTextExtractor` protocol the service is written against.

Documents are created by S3.1's real upload path wherever upload would accept
them. PDFs upload refuses — encrypted or malformed ones, or a row whose recorded
page count is wrong — are stored the way a document from before the rule existed
would be: bytes in storage, and a row whose hash matches them.
"""

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import event, text
from structlog.testing import capture_logs

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import (
    DocumentPageRepository,
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
from document_processing.validation import PDF_MIME_TYPE, content_hash
from shared.interfaces.pdf_extraction import (
    ExtractionFailure,
    PdfExtractionError,
)
from shared.interfaces.storage import StorageError, document_storage_key
from shared.models.document import DocumentType
from shared.models.extraction import ExtractedPage, ExtractedText, TextBlock
from shared.models.ingestion import IngestionJob, IngestionOutcome, IngestionReason
from shared.models.principal import Principal
from tests import extraction_helpers
from tests.pdfs import (
    make_encrypted_pdf,
    make_image_only_pdf,
    make_pages_pdf,
    make_pdf,
    make_two_column_pdf,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


# ----------------------------------------------------------------- doubles


class CountingExtractor:
    """The real extractor, counting how often — and with what — it is called."""

    def __init__(self, inner: Any = None) -> None:
        self.inner = inner or PyMuPDFTextExtractor(
            timeout_seconds=60, max_concurrency=8
        )
        self.calls = 0
        self.seen: list[bytes] = []

    async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
        self.calls += 1
        self.seen.append(data)
        result: ExtractedText = await self.inner.extract(data, max_pages=max_pages)
        return result


class FixedExtractor:
    """Returns a chosen result, or raises a chosen error, without parsing."""

    def __init__(self, result: ExtractedText | BaseException) -> None:
        self.result = result
        self.calls = 0

    async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class DuringExtractionExtractor(CountingExtractor):
    """Runs a callback while extraction is in flight, then extracts."""

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self.callback = callback

    async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
        await self.callback()
        return await super().extract(data, max_pages=max_pages)


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
    session: Any, root: Path, owner: Principal, data: bytes
) -> IngestionJob:
    """Through the real upload service; returns the job it queued."""
    queue = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=queue
    ).upload_and_process(filename="paper.pdf", stream=io.BytesIO(data), principal=owner)
    return queue.jobs[0]


async def _stored(
    session: Any,
    root: Path,
    owner: Principal,
    data: bytes,
    *,
    page_count: int | None = None,
) -> IngestionJob:
    """Bytes upload would refuse, stored as a document predating the rule would be."""
    digest = content_hash(data)
    key = document_storage_key(owner.user_id, digest)
    await LocalStorage(root).put(key, data)
    document = await DocumentRepository(session).create(
        user_id=owner.user_id,
        filename="old.pdf",
        doc_type=DocumentType.PDF,
        storage_key=key,
        content_hash=digest,
        size_bytes=len(data),
        mime_type=PDF_MIME_TYPE,
        page_count=page_count,
    )
    await session.commit()
    return IngestionJob(
        document_id=document.id,
        owner_id=owner.user_id,
        storage_key=key,
        content_hash=digest,
        mime_type=PDF_MIME_TYPE,
    )


async def _ingest(
    root: Path,
    job: IngestionJob,
    *,
    extractor: Any = None,
    storage: Any = None,
    max_pages: int | None = None,
    final: bool = False,
) -> Any:
    async with get_sessionmaker()() as session:
        return await IngestionService(
            session,
            storage or LocalStorage(root),
            extractor=extractor
            or PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=8),
            chunker=SectionAwareChunker(
                RegexTokenizer(), chunk_size=400, chunk_overlap=60
            ),
            max_pages=max_pages or get_settings().MAX_PDF_PAGES,
        ).ingest(job, final_attempt=final)


async def _row(document_id: str) -> tuple[str, str | None]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status, failure_reason FROM documents WHERE id = :id"),
                {"id": uuid.UUID(document_id)},
            )
        ).one()
    return row.status, row.failure_reason


async def _page_rows(document_id: str) -> list[tuple[int, Any]]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT page_number, blocks FROM document_pages"
                    " WHERE document_id = :id ORDER BY page_number"
                ),
                {"id": uuid.UUID(document_id)},
            )
        ).all()
    return [(row.page_number, row.blocks) for row in rows]


async def _pages(document_id: str, user_id: str) -> list[ExtractedPage]:
    async with get_sessionmaker()() as session:
        return await DocumentPageRepository(session).list_for_document(
            document_id, user_id
        )


# =============================================================================
# The happy path
# =============================================================================


class TestAVerifiedPdfIsParsedAndStored:
    async def test_every_page_is_stored_against_its_document(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(
            committing_session,
            root,
            owner,
            make_pages_pdf(["Introduction text", "Methods text", "Results text"]),
        )

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED, "parsed, then chunked"
        assert await _row(job.document_id) == ("chunked", None)
        pages = await _pages(job.document_id, owner.user_id)
        assert [(page.page_number, page.text) for page in pages] == [
            (1, "Introduction text"),
            (2, "Methods text"),
            (3, "Results text"),
        ]

    async def test_blocks_are_stored_in_reading_order_with_their_font_size(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_two_column_pdf())

        await _ingest(root, job)

        ((page_number, blocks),) = await _page_rows(job.document_id)
        assert page_number == 1
        assert [block["text"].split()[0] for block in blocks] == [
            "TITLE",
            "ABSTRACT",
            "LEFT-A",
            "LEFT-B",
            "RIGHT-A",
            "RIGHT-B",
            "FOOTER",
        ]
        assert set(blocks[0]) == {"text", "font_size"}
        assert blocks[0]["font_size"] == 18.0

    async def test_an_empty_page_is_a_stored_page_with_no_blocks(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(
            committing_session, root, owner, make_pages_pdf(["one", None, "three"])
        )

        await _ingest(root, job)

        assert [(n, len(b)) for n, b in await _page_rows(job.document_id)] == [
            (1, 1),
            (2, 0),
            (3, 1),
        ]

    async def test_the_pages_belong_to_the_owner_and_no_one_else(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        stranger = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=2))
        await _ingest(root, job)

        assert len(await _pages(job.document_id, owner.user_id)) == 2
        assert await _pages(job.document_id, stranger.user_id) == []

        await DocumentRepository(committing_session).soft_delete_for_user(
            job.document_id, owner.user_id
        )
        await committing_session.commit()
        assert await _pages(job.document_id, owner.user_id) == []

    async def test_the_api_shows_parsed_and_no_extracted_text(
        self, committing_session: Any, api_client: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(
            committing_session, root, owner, make_pdf(text="CONFIDENTIAL-FINDING")
        )
        await _ingest(root, job)
        token = JWTHandler().create_access_token(owner.user_id, owner.email, owner.role)

        response = await api_client.get(
            f"/api/v1/documents/{job.document_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "chunked"
        assert "CONFIDENTIAL-FINDING" not in response.text

    async def test_the_logs_carry_counts_never_the_text(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(
            committing_session, root, owner, make_pdf(pages=2, text="CONFIDENTIAL")
        )

        with capture_logs() as logs:
            await _ingest(root, job)

        (parsed,) = [e for e in logs if e["event"] == "ingestion.parsed"]
        assert (parsed["page_count"], parsed["pages_with_text"]) == (2, 2)
        assert "CONFIDENTIAL" not in repr(logs)
        assert str(root) not in repr(logs)


# =============================================================================
# What a PDF can be made to fail as
# =============================================================================


class TestAPdfThatCannotBeParsedFailsWithItsReason:
    async def test_a_scan_with_no_text_fails_rather_than_parsing_to_nothing(
        self, committing_session: Any, root: Path
    ) -> None:
        """Upload accepts a scan. Without OCR it can never be searched."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_image_only_pdf(2))

        result = await _ingest(root, job, final=False)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.FAILED,
            IngestionReason.NO_EXTRACTABLE_TEXT,
        )
        assert await _row(job.document_id) == ("failed", "no_extractable_text")
        assert await _page_rows(job.document_id) == []

    @pytest.mark.parametrize(
        ("data", "reason"),
        [
            (make_encrypted_pdf(), "pdf_encrypted"),
            (b"%PDF-1.7\nnot really a pdf", "pdf_open_failed"),
        ],
        ids=["encrypted", "malformed"],
    )
    async def test_a_pdf_upload_would_refuse_fails_permanently(
        self, committing_session: Any, root: Path, data: bytes, reason: str
    ) -> None:
        """The worker defends itself: the row may predate the validator."""
        owner = await _owner(committing_session)
        job = await _stored(committing_session, root, owner, data)

        result = await _ingest(root, job, final=False)

        assert result.outcome is IngestionOutcome.FAILED, "never retried"
        assert await _row(job.document_id) == ("failed", reason)
        assert await _page_rows(job.document_id) == []

    async def test_exactly_the_page_limit_is_parsed(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=3))

        result = await _ingest(root, job, max_pages=3)

        assert result.outcome is IngestionOutcome.CHUNKED

    async def test_over_the_limit_by_its_recorded_count_nothing_is_even_read(
        self, committing_session: Any, root: Path
    ) -> None:
        """A cap lowered since upload is enforced before the bytes are read."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=4))
        extractor = CountingExtractor()

        result = await _ingest(root, job, max_pages=3, extractor=extractor)

        assert result.reason is IngestionReason.PAGE_LIMIT_EXCEEDED
        assert extractor.calls == 0
        assert await _row(job.document_id) == ("failed", "page_limit_exceeded")

    async def test_over_the_limit_with_no_recorded_count_the_parser_refuses(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _stored(committing_session, root, owner, make_pdf(pages=4))

        result = await _ingest(root, job, max_pages=3)

        assert result.reason is IngestionReason.PAGE_LIMIT_EXCEEDED
        assert await _page_rows(job.document_id) == []

    async def test_a_page_count_that_disagrees_with_upload_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _stored(
            committing_session, root, owner, make_pdf(pages=2), page_count=5
        )

        result = await _ingest(root, job)

        assert result.reason is IngestionReason.PAGE_COUNT_MISMATCH
        assert await _page_rows(job.document_id) == []


# =============================================================================
# Parsing never comes before verification
# =============================================================================


class TestNothingIsParsedBeforeTheBytesAreVerified:
    async def test_a_tampered_file_is_never_parsed(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        (root / job.storage_key).write_bytes(make_pdf(text="swapped in"))
        extractor = CountingExtractor()

        result = await _ingest(root, job, extractor=extractor)

        assert result.reason is IngestionReason.CONTENT_HASH_MISMATCH
        assert extractor.calls == 0
        assert await _page_rows(job.document_id) == []

    async def test_a_missing_file_is_never_parsed(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        (root / job.storage_key).unlink()
        extractor = CountingExtractor()

        result = await _ingest(root, job, extractor=extractor)

        assert result.reason is IngestionReason.STORAGE_MISSING
        assert extractor.calls == 0

    async def test_a_storage_failure_is_retried_and_never_parsed(
        self, committing_session: Any, root: Path
    ) -> None:
        class Broken(LocalStorage):
            async def get(self, key: str) -> bytes:
                raise StorageError("unavailable")

        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        extractor = CountingExtractor()

        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, extractor=extractor, storage=Broken(root))

        assert extractor.calls == 0
        assert await _row(job.document_id) == ("pending", None)

    async def test_what_is_parsed_is_exactly_the_verified_bytes(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        data = make_pdf(pages=2)
        job = await _upload(committing_session, root, owner, data)
        extractor = CountingExtractor()

        await _ingest(root, job, extractor=extractor)

        assert extractor.seen == [data]


# =============================================================================
# Ownership
# =============================================================================


class TestOwnership:
    async def test_a_job_naming_another_owner_parses_and_stores_nothing(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        intruder = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        forged = job.model_copy(
            update={
                "owner_id": intruder.user_id,
                "storage_key": document_storage_key(intruder.user_id, job.content_hash),
            }
        )
        extractor = CountingExtractor()

        result = await _ingest(root, forged, extractor=extractor, final=True)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.REJECTED,
            IngestionReason.OWNERSHIP_MISMATCH,
        )
        assert extractor.calls == 0
        assert await _row(job.document_id) == ("pending", None)
        assert await _page_rows(job.document_id) == []

    async def test_pages_are_written_only_after_an_owner_scoped_lookup_in_sql(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
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
            i for i, s in enumerate(statements) if "INSERT INTO document_pages" in s
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


# =============================================================================
# Idempotency
# =============================================================================


class TestParsingHappensOnce:
    async def test_a_redelivered_finished_document_is_not_parsed_again(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=3))
        await _ingest(root, job)
        extractor = CountingExtractor()

        result = await _ingest(root, job, extractor=extractor)

        assert result.outcome is IngestionOutcome.SKIPPED_CHUNKED
        assert extractor.calls == 0
        assert len(await _page_rows(job.document_id)) == 3

    async def test_concurrent_deliveries_parse_once_and_store_one_copy(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=3))
        extractor = CountingExtractor()

        results = await asyncio.gather(
            *[_ingest(root, job, extractor=extractor) for _ in range(6)]
        )

        assert extractor.calls == 1
        assert [r.outcome for r in results].count(IngestionOutcome.CHUNKED) == 1
        assert [n for n, _ in await _page_rows(job.document_id)] == [1, 2, 3]

    async def test_a_retry_after_a_transient_failure_stores_one_copy(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=2))
        unavailable = FixedExtractor(
            PdfExtractionError(ExtractionFailure.PARSER_UNAVAILABLE)
        )
        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, extractor=unavailable)
        assert await _row(job.document_id) == ("pending", None)

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        assert [n for n, _ in await _page_rows(job.document_id)] == [1, 2]

    async def test_the_database_refuses_a_second_copy_of_a_page(
        self, committing_session: Any, root: Path
    ) -> None:
        """The last guard, beneath the claim and the state check."""
        from sqlalchemy.exc import IntegrityError

        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        await _ingest(root, job)

        with pytest.raises(IntegrityError):
            await DocumentPageRepository(committing_session).add_for_user(
                document_id=job.document_id,
                user_id=owner.user_id,
                pages=[ExtractedPage(page_number=1)],
            )
        await committing_session.rollback()


# =============================================================================
# Time, and failures of the parser itself
# =============================================================================


class TestAParserThatTakesTooLong:
    async def test_it_is_stopped_and_the_document_fails_as_pdf_timeout(
        self, committing_session: Any, root: Path, tmp_path: Path
    ) -> None:
        """A hostile PDF does not hold the worker: the process is killed on time.

        The extraction function sleeps instead of parsing — the one stand-in for
        a PDF that makes MuPDF spin — inside the real process extractor.
        """
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        pid_file = tmp_path / "pid"

        class Stalling:
            def __init__(self) -> None:
                self.inner = PyMuPDFTextExtractor(
                    timeout_seconds=1,
                    max_concurrency=1,
                    extract=extraction_helpers.record_pid_then_sleep,
                )

            async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
                result: ExtractedText = await self.inner.extract(
                    str(pid_file).encode(), max_pages=120
                )
                return result

        async with asyncio.timeout(30):  # a bypassed deadline fails, not hangs
            result = await _ingest(root, job, extractor=Stalling(), final=False)

        assert (result.outcome, result.reason) == (
            IngestionOutcome.FAILED,
            IngestionReason.PDF_TIMEOUT,
        )
        assert await _row(job.document_id) == ("failed", "pdf_timeout")
        assert await _page_rows(job.document_id) == []

    async def test_a_crashed_parser_is_retried_then_fails_on_the_last_attempt(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        crashing = PyMuPDFTextExtractor(
            timeout_seconds=30, max_concurrency=1, extract=extraction_helpers.die
        )

        with pytest.raises(TransientIngestionError) as raised:
            await _ingest(root, job, extractor=crashing)
        assert raised.value.reason is IngestionReason.PARSER_UNAVAILABLE
        assert await _row(job.document_id) == ("pending", None)

        result = await _ingest(root, job, extractor=crashing, final=True)

        assert result.reason is IngestionReason.PARSER_UNAVAILABLE
        assert await _row(job.document_id) == ("failed", "parser_unavailable")

    async def test_an_unexpected_extractor_error_is_retried_as_processing_failure(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf())
        broken = FixedExtractor(RuntimeError("a defect, with SECRET detail"))

        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, extractor=broken)
        result = await _ingest(root, job, extractor=broken, final=True)

        assert result.reason is IngestionReason.PROCESSING_FAILURE
        assert await _row(job.document_id) == ("failed", "processing_failure")


# =============================================================================
# Persistence: all of it, or none of it
# =============================================================================


class TestNothingIsHalfStored:
    def _unstorable(self) -> ExtractedText:
        """Page 1 is fine; page 2 holds a NUL, which PostgreSQL's JSONB refuses.

        The real extractor strips NUL. This stands in for any write that fails
        after some rows have already been sent.
        """
        return ExtractedText(
            page_count=2,
            pages=(
                ExtractedPage(
                    page_number=1, blocks=(TextBlock(text="fine", font_size=10),)
                ),
                ExtractedPage(
                    page_number=2, blocks=(TextBlock(text="bad\x00", font_size=10),)
                ),
            ),
        )

    async def test_a_failed_write_rolls_back_every_page_and_the_status(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _stored(committing_session, root, owner, make_pdf(pages=2))

        with pytest.raises(TransientIngestionError) as raised:
            await _ingest(root, job, extractor=FixedExtractor(self._unstorable()))

        assert raised.value.reason is IngestionReason.PROCESSING_FAILURE
        assert await _row(job.document_id) == ("pending", None)
        assert await _page_rows(job.document_id) == []

    async def test_the_document_stays_recoverable_and_parses_next_time(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _stored(committing_session, root, owner, make_pdf(pages=2))
        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, extractor=FixedExtractor(self._unstorable()))

        result = await _ingest(root, job)

        assert result.outcome is IngestionOutcome.CHUNKED
        assert [n for n, _ in await _page_rows(job.document_id)] == [1, 2]

    async def test_on_the_last_attempt_it_fails_with_nothing_stored(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _stored(committing_session, root, owner, make_pdf(pages=2))

        result = await _ingest(
            root, job, extractor=FixedExtractor(self._unstorable()), final=True
        )

        assert result.reason is IngestionReason.PROCESSING_FAILURE
        assert await _row(job.document_id) == ("failed", "processing_failure")
        assert await _page_rows(job.document_id) == []

    async def test_a_claim_reaped_during_extraction_stores_nothing(
        self, committing_session: Any, root: Path
    ) -> None:
        """The reaper decided this delivery was dead. Its pages must not land."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner, make_pdf(pages=2))

        async def reaper_intervenes() -> None:
            async with get_sessionmaker()() as other:
                await other.execute(
                    text(
                        "UPDATE documents SET updated_at = now() - interval '1 day'"
                        " WHERE id = :id"
                    ),
                    {"id": uuid.UUID(job.document_id)},
                )
                await other.commit()
                await IngestionService(
                    other,
                    LocalStorage(root),
                    extractor=CountingExtractor(),
                    chunker=SectionAwareChunker(
                        RegexTokenizer(), chunk_size=400, chunk_overlap=60
                    ),
                    max_pages=10,
                ).reap_stale_processing(stale_after_seconds=60)

        result = await _ingest(
            root, job, extractor=DuringExtractionExtractor(reaper_intervenes)
        )

        assert result.outcome is IngestionOutcome.CLAIM_LOST
        assert await _row(job.document_id) == ("failed", "stale_processing")
        assert await _page_rows(job.document_id) == []
