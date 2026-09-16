"""Retrieval against real PostgreSQL and real Qdrant — M4/S4.1.

ADR-0003 §5's *correct path*, which is the whole reason this sprint exists: a
candidate from the index means nothing until the database agrees. A mocked
database would prove only that the mock agrees.

Documents are put into each state through the real ingestion pipeline where
that is what a test is about, and by writing the row directly where the test is
about retrieval's reaction to a state rather than how the state came about.
"""

import dataclasses
import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import text

from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import DocumentChunkRepository, UserRepository
from backend.db.session import get_sessionmaker
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import IngestionService
from backend.services.search_service import (
    InvalidSearchQuery,
    SearchService,
    SearchUnavailable,
)
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from document_processing.tokenization import TOKENIZER_ID, RegexTokenizer
from shared.models.document import DocumentStatus
from shared.models.ingestion import IngestionJob
from shared.models.principal import Principal
from shared.models.retrieval import RetrievalQuery
from tests.doubles import (
    STUB_DIMENSION,
    STUB_MODEL_ID,
    InMemoryVectorStore,
    StubEmbeddingProvider,
)
from tests.pdfs import make_paper_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.qdrant,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


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


async def _ingest(session: Any, root: Path, owner: Principal, vectors: Any) -> str:
    """Upload and run the real pipeline to `ready`. Returns the document id."""
    queue = RecordingQueue()
    await DocumentService(
        session, storage=LocalStorage(root), ingestion=queue
    ).upload_and_process(
        filename="paper.pdf", stream=io.BytesIO(make_paper_pdf()), principal=owner
    )
    job = queue.jobs[0]
    async with get_sessionmaker()() as worker_session:
        await IngestionService(
            worker_session,
            LocalStorage(root),
            extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=4),
            chunker=SectionAwareChunker(
                RegexTokenizer(), chunk_size=120, chunk_overlap=20
            ),
            embedder=StubEmbeddingProvider(),
            vectors=vectors,
            strategy_version=STRATEGY_VERSION,
            tokenizer_id=TOKENIZER_ID,
            max_pages=get_settings().MAX_PDF_PAGES,
        ).ingest(job, final_attempt=False)
    return job.document_id


def _search_service(session: Any, vectors: Any, **kwargs: Any) -> SearchService:
    settings = get_settings()
    return SearchService(
        session,
        embedder=kwargs.pop("embedder", None) or StubEmbeddingProvider(),
        vectors=vectors,
        top_k=kwargs.pop("top_k", settings.RETRIEVAL_TOP_K),
        score_threshold=kwargs.pop("score_threshold", 0.0),
        max_query_chars=settings.RETRIEVAL_MAX_QUERY_CHARS,
    )


async def _search(vectors: Any, owner: Principal, **kwargs: Any) -> Any:
    async with get_sessionmaker()() as session:
        service = _search_service(session, vectors, **kwargs)
        return await service.search(
            RetrievalQuery(text=kwargs.pop("query", "retrieval quality")), owner
        )


async def _set(document_id: str, **values: Any) -> None:
    assignments = ", ".join(f"{k} = :{k}" for k in values)
    async with get_sessionmaker()() as session:
        await session.execute(
            text(f"UPDATE documents SET {assignments} WHERE id = :id"),
            {"id": uuid.UUID(document_id), **values},
        )
        await session.commit()


async def _chunk_ids(document_id: str) -> list[str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT id FROM document_chunks WHERE document_id = :id"
                " ORDER BY chunk_index"
            ),
            {"id": uuid.UUID(document_id)},
        )
        return [str(row[0]) for row in rows.all()]


class TestASearchableDocumentIsFound:
    async def test_a_ready_document_is_retrieved(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)

        results = await _search(vector_store, owner)

        assert results
        assert {r.document_id for r in results} == {document_id}

    async def test_the_text_comes_from_postgresql(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """ADR-0013 §5 keeps text out of the payload, so this is the only
        place it can have come from — and it arrives already validated."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)

        results = await _search(vector_store, owner)

        async with get_sessionmaker()() as session:
            stored = {
                chunk.id: chunk.content
                for chunk in await DocumentChunkRepository(session).list_for_document(
                    document_id, owner.user_id
                )
            }
        assert results
        for result in results:
            assert result.content == stored[result.chunk_id]

    async def test_a_result_carries_the_provenance_a_citation_needs(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        await _ingest(committing_session, root, owner, vector_store)

        (first, *_rest) = await _search(vector_store, owner)

        assert first.page_start >= 1
        assert first.page_end >= first.page_start
        assert first.chunk_index >= 0
        assert first.embedding_model_id == STUB_MODEL_ID
        assert first.content.strip()

    async def test_no_qdrant_or_provider_object_reaches_the_caller(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        await _ingest(committing_session, root, owner, vector_store)

        (first, *_rest) = await _search(vector_store, owner)

        rendered = first.model_dump()
        assert set(rendered) == {
            "chunk_id",
            "document_id",
            "score",
            "content",
            "chunk_index",
            "section",
            "page_start",
            "page_end",
            "embedding_model_id",
        }


class TestPostgresqlDecidesEligibility:
    @pytest.mark.parametrize(
        "status",
        [
            DocumentStatus.PENDING,
            DocumentStatus.PROCESSING,
            DocumentStatus.PARSED,
            DocumentStatus.CHUNKED,
            DocumentStatus.FAILED,
        ],
    )
    async def test_only_ready_is_searchable(
        self,
        committing_session: Any,
        root: Path,
        vector_store: Any,
        status: DocumentStatus,
    ) -> None:
        """The vectors are still there. The status is what decides."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)
        assert await _search(vector_store, owner)

        await _set(
            document_id,
            status=status.value,
            failure_reason="unknown" if status is DocumentStatus.FAILED else None,
        )

        assert await _search(vector_store, owner) == ()

    async def test_a_deleted_document_is_not_searchable(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """The soft delete commits first and the vectors stay (ADR-0003 §6 is
        half-implemented). This is what makes that safe."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)
        assert await _search(vector_store, owner)

        await DocumentService(
            committing_session, storage=LocalStorage(root), ingestion=RecordingQueue()
        ).delete_document(document_id, owner)

        assert await vector_store.count_for_document(
            document_id=document_id, owner_id=owner.user_id
        )
        assert await _search(vector_store, owner) == ()

    async def test_a_missing_chunk_is_dropped(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)
        gone = (await _chunk_ids(document_id))[0]
        async with get_sessionmaker()() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE id = :id"),
                {"id": uuid.UUID(gone)},
            )
            await session.commit()

        results = await _search(vector_store, owner)

        assert gone not in {r.chunk_id for r in results}

    async def test_a_chunk_embedded_by_another_model_is_dropped(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """Its score comes from a different vector space (ADR-0014 §4)."""
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)
        async with get_sessionmaker()() as session:
            await session.execute(
                text(
                    "UPDATE document_chunks SET embedding_model_id = 'other/v9'"
                    " WHERE document_id = :id"
                ),
                {"id": uuid.UUID(document_id)},
            )
            await session.commit()

        assert await _search(vector_store, owner) == ()

    async def test_a_chunk_of_another_width_is_dropped(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)
        async with get_sessionmaker()() as session:
            await session.execute(
                text(
                    "UPDATE document_chunks SET dimension = :d WHERE document_id = :id"
                ),
                {"d": STUB_DIMENSION + 1, "id": uuid.UUID(document_id)},
            )
            await session.commit()

        assert await _search(vector_store, owner) == ()

    async def test_a_chunk_id_that_exists_nowhere_is_dropped(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        async with get_sessionmaker()() as session:
            validated = await DocumentChunkRepository(session).validate_for_retrieval(
                [str(uuid.uuid4())],
                owner.user_id,
                embedding_model_id=STUB_MODEL_ID,
                dimension=STUB_DIMENSION,
            )
        assert validated == {}

    async def test_a_malformed_chunk_id_is_dropped_not_raised(
        self, committing_session: Any
    ) -> None:
        """Ids reach here from payloads and, later, tool arguments."""
        owner = await _owner(committing_session)
        async with get_sessionmaker()() as session:
            validated = await DocumentChunkRepository(session).validate_for_retrieval(
                ["../../etc/passwd", "", "not-a-uuid"],
                owner.user_id,
                embedding_model_id=STUB_MODEL_ID,
                dimension=STUB_DIMENSION,
            )
        assert validated == {}


class TestTenantIsolation:
    """Principal.user_id == Qdrant owner_id == documents.user_id, or nothing."""

    @pytest.fixture()
    async def two_corpora(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> tuple[Principal, str, Principal, str]:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)
        alice_document = await _ingest(committing_session, root, alice, vector_store)
        bob_document = await _ingest(committing_session, root, bob, vector_store)
        return alice, alice_document, bob, bob_document

    async def test_each_owner_sees_only_their_own(
        self, vector_store: Any, two_corpora: tuple[Principal, str, Principal, str]
    ) -> None:
        """Identical PDFs, so the vectors are identical and only ownership differs."""
        alice, alice_document, bob, bob_document = two_corpora

        alice_results = await _search(vector_store, alice)
        bob_results = await _search(vector_store, bob)

        assert alice_results and bob_results
        assert {r.document_id for r in alice_results} == {alice_document}
        assert {r.document_id for r in bob_results} == {bob_document}

    async def test_naming_another_owners_document_returns_nothing(
        self, vector_store: Any, two_corpora: tuple[Principal, str, Principal, str]
    ) -> None:
        _alice, alice_document, bob, _bob_document = two_corpora

        results = await _search(vector_store, bob, query="retrieval quality")
        scoped = await _search_document_ids(vector_store, bob, [alice_document])

        assert results
        assert scoped == ()

    async def test_a_stale_payload_claiming_another_owner_grants_nothing(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        """The attack this design exists to defeat.

        Alice's chunk is re-upserted with a payload that says it is Bob's.
        Qdrant then hands it to Bob's search — and PostgreSQL, which is asked
        only for the chunk *id*, says it is Alice's, so Bob gets nothing.
        """
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)
        alice_document = await _ingest(committing_session, root, alice, vector_store)

        stolen = (await _chunk_ids(alice_document))[0]
        found = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=alice.user_id,
            limit=100,
            score_threshold=-1.0,
        )
        original = next(match for match in found if match.id == stolen)
        forged = dataclasses.replace(original.payload, user_id=bob.user_id)
        from shared.interfaces.vector_store import VectorRecord

        await vector_store.upsert(
            [
                VectorRecord(
                    id=stolen,
                    vector=await StubEmbeddingProvider().embed_query("anything"),
                    payload=forged,
                )
            ]
        )

        # Qdrant genuinely offers it to Bob now.
        offered = await vector_store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=bob.user_id,
            limit=100,
            score_threshold=-1.0,
        )
        assert stolen in {match.id for match in offered}

        # The service does not: PostgreSQL is asked for the chunk *id*, and
        # the row says Alice.
        assert await _search(vector_store, bob) == ()

        # And Alice loses it from her own results, because the payload the
        # owner filter reads no longer says she owns it. That is the documented
        # degradation exactly — "fewer results, never leaked another user's
        # manuscript" (ADR-0003 Consequences). A corrupted index can hide a
        # chunk; it cannot hand one over.
        assert stolen not in {r.chunk_id for r in await _search(vector_store, alice)}
        assert await _search(vector_store, alice) != ()


async def _search_document_ids(
    vectors: Any, owner: Principal, document_ids: list[str]
) -> Any:
    async with get_sessionmaker()() as session:
        return await _search_service(session, vectors).search(
            RetrievalQuery(text="retrieval quality", document_ids=tuple(document_ids)),
            owner,
        )


class TestOrderingSurvivesValidation:
    async def test_the_index_order_is_preserved_after_drops(
        self, committing_session: Any, root: Path, vector_store: Any
    ) -> None:
        await vector_store.ensure_collection(dimension=STUB_DIMENSION)
        owner = await _owner(committing_session)
        document_id = await _ingest(committing_session, root, owner, vector_store)

        before = await _search(vector_store, owner)
        assert len(before) >= 3

        # Remove the middle chunk from the database only; its vector stays.
        middle = before[1].chunk_id
        async with get_sessionmaker()() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE id = :id"),
                {"id": uuid.UUID(middle)},
            )
            await session.commit()

        after = await _search(vector_store, owner)

        assert [r.chunk_id for r in after] == [
            r.chunk_id for r in before if r.chunk_id != middle
        ]
        assert [r.score for r in after] == sorted(
            (r.score for r in after), reverse=True
        )
        assert document_id


class TestFailuresAreNotEmptyResults:
    async def test_an_unreachable_index_raises(
        self, committing_session: Any, root: Path
    ) -> None:
        """A broken Qdrant must not look like a corpus with no matches."""
        owner = await _owner(committing_session)
        vectors = InMemoryVectorStore()
        await vectors.ensure_collection(dimension=STUB_DIMENSION)

        class Unreachable(InMemoryVectorStore):
            async def search(self, *args: Any, **kwargs: Any) -> Any:
                from shared.interfaces.vector_store import VectorStoreError

                raise VectorStoreError("could not reach qdrant at qdrant:6333")

        with pytest.raises(SearchUnavailable) as raised:
            await _search(Unreachable(), owner)
        assert "6333" not in str(raised.value)

    async def test_an_invalid_query_never_reaches_the_database(
        self, committing_session: Any, vector_store: Any
    ) -> None:
        owner = await _owner(committing_session)
        with pytest.raises(InvalidSearchQuery):
            await _search(vector_store, owner, query="   ")


class TestWithTheRealModel:
    """The whole path, with nothing substituted: real PostgreSQL, real Qdrant,
    real FastEmbed, and the tokenizer that sized the chunks doing the sizing."""

    @pytest.mark.embeddings
    async def test_a_question_finds_the_passage_that_answers_it(
        self,
        committing_session: Any,
        root: Path,
        vector_store: Any,
        embedding_provider: Any,
    ) -> None:
        await vector_store.ensure_collection(dimension=embedding_provider.dimension)
        owner = await _owner(committing_session)

        queue = RecordingQueue()
        await DocumentService(
            committing_session, storage=LocalStorage(root), ingestion=queue
        ).upload_and_process(
            filename="paper.pdf",
            stream=io.BytesIO(make_paper_pdf()),
            principal=owner,
        )
        job = queue.jobs[0]
        async with get_sessionmaker()() as worker_session:
            result = await IngestionService(
                worker_session,
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
        assert result.outcome.value == "ready"

        async with get_sessionmaker()() as session:
            results = await _search_service(
                session, vector_store, embedder=embedding_provider
            ).search(RetrievalQuery(text="What did the study measure?"), owner)

        assert results
        assert {r.document_id for r in results} == {job.document_id}
        assert {r.embedding_model_id for r in results} == {embedding_provider.model_id}
        assert [r.score for r in results] == sorted(
            (r.score for r in results), reverse=True
        )
        assert all(r.content.strip() for r in results)

    @pytest.mark.embeddings
    async def test_two_owners_of_the_same_paper_never_meet(
        self,
        committing_session: Any,
        root: Path,
        vector_store: Any,
        embedding_provider: Any,
    ) -> None:
        """Identical bytes, identical text, identical vectors — and the only
        thing separating them is the invariant this sprint implements."""
        await vector_store.ensure_collection(dimension=embedding_provider.dimension)
        alice = await _owner(committing_session)
        bob = await _owner(committing_session)

        documents = {}
        for owner in (alice, bob):
            queue = RecordingQueue()
            await DocumentService(
                committing_session, storage=LocalStorage(root), ingestion=queue
            ).upload_and_process(
                filename="paper.pdf",
                stream=io.BytesIO(make_paper_pdf()),
                principal=owner,
            )
            job = queue.jobs[0]
            documents[owner.user_id] = job.document_id
            async with get_sessionmaker()() as worker_session:
                await IngestionService(
                    worker_session,
                    LocalStorage(root),
                    extractor=PyMuPDFTextExtractor(
                        timeout_seconds=60, max_concurrency=4
                    ),
                    chunker=SectionAwareChunker(
                        embedding_provider.tokenizer, chunk_size=120, chunk_overlap=20
                    ),
                    embedder=embedding_provider,
                    vectors=vector_store,
                    strategy_version=STRATEGY_VERSION,
                    tokenizer_id=embedding_provider.tokenizer.id,
                    max_pages=get_settings().MAX_PDF_PAGES,
                ).ingest(job, final_attempt=False)

        for owner in (alice, bob):
            async with get_sessionmaker()() as session:
                results = await _search_service(
                    session, vector_store, embedder=embedding_provider
                ).search(RetrievalQuery(text="What did the study measure?"), owner)
            assert results
            assert {r.document_id for r in results} == {documents[owner.user_id]}
