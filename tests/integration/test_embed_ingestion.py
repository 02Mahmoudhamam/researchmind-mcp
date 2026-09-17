"""Embedding inside ingestion, against real PostgreSQL and real Qdrant — M3/S3.5.

What `IngestionService` does with the embed stage: when `ready` may be set and
what must be true first, what happens to chunks a different tokenizer sized,
what each failure becomes, and what a crash between the two stores leaves
behind.

The vector store is a **real Qdrant collection** built for each test, because
the properties that matter — that a tenant cannot reach another's vectors, that
the same chunk written twice is one point — are the server's and not a
double's. The provider is usually the stub (`tests/doubles.py`): it is
deterministic, and these tests are about the pipeline rather than about the
model, which `tests/unit/test_embeddings.py` covers. One test runs the real
model end to end.
"""

import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import pytest
from sqlalchemy import text

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import UserRepository
from backend.db.session import get_sessionmaker
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import (
    IngestionService,
    TransientIngestionError,
)
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from document_processing.tokenization import TOKENIZER_ID, RegexTokenizer
from shared.models.chunking import chunk_id_for
from shared.models.document import DocumentStatus
from shared.models.ingestion import IngestionJob, IngestionOutcome, IngestionReason
from shared.models.principal import Principal
from tests.doubles import (
    STUB_DIMENSION,
    STUB_MODEL_ID,
    FailingEmbeddingProvider,
    InMemoryVectorStore,
    StubEmbeddingProvider,
)
from tests.pdfs import make_paper_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.qdrant,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

# The tokenizer a document was chunked with before this pipeline's.
OLD_TOKENIZER_ID = "regex-word/v0"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


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


async def _upload(session: Any, root: Path, owner: Principal) -> IngestionJob:
    queue = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=queue
    ).upload_and_process(
        filename="paper.pdf", stream=io.BytesIO(make_paper_pdf()), principal=owner
    )
    return queue.jobs[0]


def _service(
    session: Any,
    root: Path,
    *,
    vectors: Any,
    embedder: Any = None,
    tokenizer_id: str = TOKENIZER_ID,
    strategy_version: str = STRATEGY_VERSION,
) -> IngestionService:
    return IngestionService(
        session,
        LocalStorage(root),
        extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=4),
        chunker=SectionAwareChunker(RegexTokenizer(), chunk_size=120, chunk_overlap=20),
        embedder=embedder or StubEmbeddingProvider(),
        vectors=vectors,
        strategy_version=strategy_version,
        tokenizer_id=tokenizer_id,
        max_pages=get_settings().MAX_PDF_PAGES,
    )


async def _ingest(root: Path, job: IngestionJob, *, final: bool = False, **kwargs: Any):
    async with get_sessionmaker()() as session:
        return await _service(session, root, **kwargs).ingest(job, final_attempt=final)


async def _row(document_id: str) -> tuple[str, str | None, int | None]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT status, failure_reason, chunk_count FROM documents"
                " WHERE id = :id"
            ),
            {"id": uuid.UUID(document_id)},
        )
        status, reason, count = result.one()
        return str(status), reason, count


async def _chunks(document_id: str) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT id, chunk_index, tokenizer_id, strategy_version,"
                " embedding_model_id, dimension FROM document_chunks"
                " WHERE document_id = :id ORDER BY chunk_index"
            ),
            {"id": uuid.UUID(document_id)},
        )
        return [dict(row._mapping) for row in result.all()]


async def _set_status(document_id: str, status: DocumentStatus) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE documents SET status = :s WHERE id = :id"),
            {"s": status.value, "id": uuid.UUID(document_id)},
        )
        await session.commit()


async def _set_tokenizer(document_id: str, tokenizer_id: str) -> None:
    """Rewrite the provenance, as an older pipeline would have left it."""
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE document_chunks SET tokenizer_id = :t WHERE document_id = :id"
            ),
            {"t": tokenizer_id, "id": uuid.UUID(document_id)},
        )
        await session.commit()


class TestASoundDocumentBecomesReady:
    async def test_one_delivery_carries_it_to_ready(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        result = await _ingest(root, job, vectors=vector_store)

        assert result.outcome is IngestionOutcome.READY
        assert (await _row(job.document_id))[:2] == ("ready", None)

    async def test_the_vectors_are_in_the_store(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job, vectors=vector_store)

        stored = await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        )
        assert stored == len(await _chunks(job.document_id)) > 0

    async def test_every_chunk_records_the_model_that_embedded_it(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """A document that says it is searchable must say what made it so."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job, vectors=vector_store)

        rows = await _chunks(job.document_id)
        assert rows
        assert {row["embedding_model_id"] for row in rows} == {STUB_MODEL_ID}
        assert {row["dimension"] for row in rows} == {STUB_DIMENSION}

    async def test_a_points_id_is_its_chunks_id(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """ADR-0003 and ADR-0013 §4 — and it is derived, not drawn."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job, vectors=vector_store)

        rows = await _chunks(job.document_id)
        for row in rows:
            assert row["id"] == chunk_id_for(
                document_id=job.document_id,
                chunk_index=row["chunk_index"],
                strategy_version=STRATEGY_VERSION,
                tokenizer_id=TOKENIZER_ID,
            )
        found = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=owner.user_id,
            limit=100,
            score_threshold=-1.0,
        )
        assert {match.id for match in found} == {str(row["id"]) for row in rows}

    async def test_the_payload_carries_the_citation_and_no_text(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        await _ingest(root, job, vectors=vector_store)

        found = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=owner.user_id,
            limit=100,
            score_threshold=-1.0,
        )
        payload = min(found, key=lambda match: match.payload.chunk_index).payload
        assert payload.user_id == owner.user_id
        assert payload.document_id == job.document_id
        assert payload.embedding_model_id == STUB_MODEL_ID
        assert payload.tokenizer_id == TOKENIZER_ID
        assert payload.strategy_version == STRATEGY_VERSION
        assert payload.page_start >= 1 and payload.page_end >= payload.page_start


class TestReadyRequiresTheVectors:
    async def test_a_store_that_refuses_leaves_the_document_chunked(
        self, committing_session: Any, root: Path
    ) -> None:
        """ADR-0013 §3: `ready` with no vectors is the one state a client
        cannot detect, so the upsert must happen first and must be believed."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        vectors = InMemoryVectorStore()
        vectors.fail_upsert = True

        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, vectors=vectors)

        assert (await _row(job.document_id))[0] == "chunked"
        assert vectors.points == {}

    async def test_the_last_attempt_records_the_vector_store_failure(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        vectors = InMemoryVectorStore()
        vectors.fail_upsert = True

        result = await _ingest(root, job, vectors=vectors, final=True)

        assert result.reason is IngestionReason.VECTOR_STORE_FAILURE
        assert (await _row(job.document_id))[:2] == (
            "failed",
            "vector_store_failure",
        )

    async def test_an_embedding_failure_is_retried_from_chunked(
        self, committing_session: Any, root: Path
    ) -> None:
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        with pytest.raises(TransientIngestionError):
            await _ingest(
                root,
                job,
                vectors=InMemoryVectorStore(),
                embedder=FailingEmbeddingProvider(),
            )

        # Released to where the stage started: a retry embeds, never re-parses.
        assert (await _row(job.document_id))[0] == "chunked"

    async def test_a_retry_after_an_embedding_failure_succeeds(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        with pytest.raises(TransientIngestionError):
            await _ingest(
                root,
                job,
                vectors=vector_store,
                embedder=FailingEmbeddingProvider(),
            )

        result = await _ingest(root, job, vectors=vector_store)

        assert result.outcome is IngestionOutcome.READY

    async def test_a_chunked_document_with_no_chunks_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        """`chunked` says its chunks were stored; they are not there now."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=InMemoryVectorStore())
        async with get_sessionmaker()() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE document_id = :id"),
                {"id": uuid.UUID(job.document_id)},
            )
            await session.commit()
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        result = await _ingest(root, job, vectors=InMemoryVectorStore())

        assert result.reason is IngestionReason.NO_CHUNKS_TO_EMBED
        assert (await _row(job.document_id))[:2] == ("failed", "no_chunks_to_embed")

    async def test_a_vector_of_the_wrong_width_fails_permanently(
        self, committing_session: Any, root: Path
    ) -> None:
        """Retrying the same model against the same collection cannot help."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        class WrongWidth(StubEmbeddingProvider):
            async def embed_documents(self, texts: Sequence[str]) -> Any:
                return tuple((0.1, 0.2) for _ in texts)

        result = await _ingest(
            root, job, vectors=InMemoryVectorStore(), embedder=WrongWidth()
        )

        assert result.reason is IngestionReason.EMBEDDING_DIMENSION_MISMATCH
        assert (await _row(job.document_id))[:2] == (
            "failed",
            "embedding_dimension_mismatch",
        )


class TestAVersionChangeRechunks:
    """ADR-0013 §2. The promise ADR-0012 §2 made when it called the tokenizer
    provisional: what needs re-running is a query, not a dated script."""

    async def test_chunks_from_another_tokenizer_are_made_again(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        await _set_tokenizer(job.document_id, OLD_TOKENIZER_ID)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        result = await _ingest(root, job, vectors=vector_store)

        assert result.outcome is IngestionOutcome.READY
        rows = await _chunks(job.document_id)
        assert {row["tokenizer_id"] for row in rows} == {TOKENIZER_ID}

    async def test_the_old_vectors_are_deleted_not_left_beside_the_new(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """Their ids came from the old tokenizer, so an upsert cannot overwrite
        them — they would linger, searchable and attributed to this document."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        before = await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        )
        await _set_tokenizer(job.document_id, OLD_TOKENIZER_ID)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        await _ingest(root, job, vectors=vector_store)

        after = await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        )
        assert after == before == len(await _chunks(job.document_id))

    async def test_mixed_provenance_is_treated_as_stale(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """An interrupted re-chunk left two generations. Neither is trusted."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        async with get_sessionmaker()() as session:
            await session.execute(
                text(
                    "UPDATE document_chunks SET tokenizer_id = :t"
                    " WHERE document_id = :id AND chunk_index = 0"
                ),
                {"t": OLD_TOKENIZER_ID, "id": uuid.UUID(job.document_id)},
            )
            await session.commit()
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        await _ingest(root, job, vectors=vector_store)

        rows = await _chunks(job.document_id)
        assert {row["tokenizer_id"] for row in rows} == {TOKENIZER_ID}

    async def test_matching_provenance_is_not_rechunked(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """Re-chunking what is already correct would re-embed it at cost."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        before = await _chunks(job.document_id)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        await _ingest(root, job, vectors=vector_store)

        assert await _chunks(job.document_id) == before

    async def test_the_chunk_count_follows_the_new_chunks(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        await _set_tokenizer(job.document_id, OLD_TOKENIZER_ID)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        await _ingest(root, job, vectors=vector_store)

        assert (await _row(job.document_id))[2] == len(await _chunks(job.document_id))

    async def test_a_stale_document_whose_pages_are_gone_fails(
        self, committing_session: Any, root: Path
    ) -> None:
        """Embedding the old chunks would bake in another ruler's boundaries."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=InMemoryVectorStore())
        async with get_sessionmaker()() as session:
            await session.execute(
                text("DELETE FROM document_pages WHERE document_id = :id"),
                {"id": uuid.UUID(job.document_id)},
            )
            await session.commit()
        await _set_tokenizer(job.document_id, OLD_TOKENIZER_ID)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        result = await _ingest(root, job, vectors=InMemoryVectorStore())

        assert result.reason is IngestionReason.NO_CHUNKS_TO_EMBED

    async def test_a_store_that_cannot_delete_stops_before_rechunking(
        self, committing_session: Any, root: Path
    ) -> None:
        """Writing new chunks first would strand the old vectors for good."""
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        vectors = InMemoryVectorStore()
        await _ingest(root, job, vectors=vectors)
        before = await _chunks(job.document_id)
        await _set_tokenizer(job.document_id, OLD_TOKENIZER_ID)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)
        vectors.fail_delete = True

        with pytest.raises(TransientIngestionError):
            await _ingest(root, job, vectors=vectors)

        assert len(await _chunks(job.document_id)) == len(before)
        assert (await _row(job.document_id))[0] == "chunked"


class TestIdempotency:
    async def test_embedding_the_same_document_twice_stores_one_set(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """A redelivery, a restart, or a crash after the upsert."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        once = await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        )
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        await _ingest(root, job, vectors=vector_store)

        assert (
            await vector_store.count_for_document(
                document_id=job.document_id, owner_id=owner.user_id
            )
            == once
        )

    async def test_a_crash_between_the_stores_converges_on_a_retry(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """The order ADR-0013 §3 chose: Qdrant, then PostgreSQL.

        The vectors are written and the commit never happens. The document is
        still `chunked`, the points are there, and the retry upserts the same
        derived ids over the same points and commits.
        """
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        await _set_status(job.document_id, DocumentStatus.CHUNKED)

        class CrashesAfterUpsert:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

            async def upsert(self, records: Any) -> None:
                await self.inner.upsert(records)
                raise TimeoutError("the process died here")

        with pytest.raises(TimeoutError):
            await _ingest(root, job, vectors=CrashesAfterUpsert(vector_store))
        assert (await _row(job.document_id))[0] == "processing"
        stranded = await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        )
        assert stranded > 0

        # What the reaper or a recovery sweep would hand back.
        await _set_status(job.document_id, DocumentStatus.CHUNKED)
        result = await _ingest(root, job, vectors=vector_store)

        assert result.outcome is IngestionOutcome.READY
        assert (
            await vector_store.count_for_document(
                document_id=job.document_id, owner_id=owner.user_id
            )
            == stranded
        )

    async def test_a_ready_document_is_left_alone(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)
        await _ingest(root, job, vectors=vector_store)
        before = await _chunks(job.document_id)

        result = await _ingest(root, job, vectors=vector_store)

        assert result.outcome is IngestionOutcome.SKIPPED_TERMINAL
        assert await _chunks(job.document_id) == before


class TestTenantsStaySeparate:
    async def test_two_owners_of_the_same_paper_get_their_own_vectors(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """Identical bytes, identical chunks, and still not each other's."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)
        alice_job = await _upload(committing_session, root, alice)
        bob_job = await _upload(committing_session, root, bob)

        await _ingest(root, alice_job, vectors=vector_store)
        await _ingest(root, bob_job, vectors=vector_store)

        found = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=alice.user_id,
            limit=100,
            score_threshold=-1.0,
        )
        assert found
        assert {match.payload.user_id for match in found} == {alice.user_id}
        assert {match.payload.document_id for match in found} == {alice_job.document_id}

    async def test_naming_another_owners_document_returns_nothing(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)
        alice_job = await _upload(committing_session, root, alice)
        await _ingest(root, alice_job, vectors=vector_store)

        found = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=bob.user_id,
            limit=100,
            score_threshold=-1.0,
            document_ids=[alice_job.document_id],
        )

        assert found == ()

    async def test_a_job_naming_the_wrong_owner_writes_no_vector(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """The owner-scoped read finds nothing, so the stage never runs."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)
        job = await _upload(committing_session, root, alice)
        forged = job.model_copy(update={"owner_id": bob.user_id})

        result = await _ingest(root, forged, vectors=vector_store)

        assert result.outcome is IngestionOutcome.REJECTED
        assert (
            await vector_store.count_for_document(
                document_id=job.document_id, owner_id=alice.user_id
            )
            == 0
        )


class TestTheRealModel:
    @pytest.mark.embeddings
    async def test_a_paper_is_embedded_by_the_model_that_sized_its_chunks(
        self,
        committing_session: Any,
        root: Path,
        vector_store: Any,
        embedding_provider: Any,
    ) -> None:
        """End to end with the real provider, its tokenizer, and real Qdrant.

        The chunker counts with the model's own vocabulary here, so the chunks
        it produces are the chunks the model reads (ADR-0013 §1).
        """
        await vector_store.ensure_collection(dimension=embedding_provider.dimension)
        owner = await _owner(committing_session)
        job = await _upload(committing_session, root, owner)

        async with get_sessionmaker()() as session:
            result = await IngestionService(
                session,
                LocalStorage(root),
                extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=4),
                chunker=SectionAwareChunker(
                    embedding_provider.tokenizer, chunk_size=120, chunk_overlap=20
                ),
                embedder=embedding_provider,
                vectors=vector_store,
                strategy_version=STRATEGY_VERSION,
                tokenizer_id=embedding_provider.tokenizer.id,
                max_pages=get_settings().MAX_PDF_PAGES,
            ).ingest(job, final_attempt=False)

        assert result.outcome is IngestionOutcome.READY
        rows = await _chunks(job.document_id)
        assert {row["tokenizer_id"] for row in rows} == {"bge-small-en-v1.5/wordpiece"}
        assert {row["embedding_model_id"] for row in rows} == {
            embedding_provider.model_id
        }
        assert {row["dimension"] for row in rows} == {embedding_provider.dimension}
        assert await vector_store.count_for_document(
            document_id=job.document_id, owner_id=owner.user_id
        ) == len(rows)
