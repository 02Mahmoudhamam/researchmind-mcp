"""What `SearchService` does before and after it touches a database — M4/S4.1.

Query validation, the order calls happen in, what it asks the vector store for,
ordering, deduplication, and the line between "no matches" and "the index is
down". The PostgreSQL half is in `tests/integration/test_retrieval.py`, against
a real database; here the chunk repository is replaced so these stay fast and
so a failure names one thing.

ADR-0014; ADR-0003 §5; `docs/security/principles.md` §3.
"""

import pytest

from backend.services.search_service import (
    InvalidSearchQuery,
    SearchService,
    SearchUnavailable,
)
from shared.interfaces.embedding import SPECIAL_TOKENS_PER_SEQUENCE, EmbeddingError
from shared.interfaces.vector_store import VectorMatch, VectorPayload, VectorStoreError
from shared.models.principal import Principal
from shared.models.retrieval import RetrievalQuery, ValidatedChunk
from shared.models.user import UserRole
from tests.doubles import STUB_DIMENSION, InMemoryVectorStore, StubEmbeddingProvider

ALICE = Principal(
    user_id="11111111-1111-4111-8111-111111111111",
    email="alice@example.com",
    role=UserRole.RESEARCHER,
)
BOB = Principal(
    user_id="22222222-2222-4222-8222-222222222222",
    email="bob@example.com",
    role=UserRole.RESEARCHER,
)
DOCUMENT = "33333333-3333-4333-8333-333333333333"


def _chunk(chunk_id: str, index: int = 0) -> ValidatedChunk:
    return ValidatedChunk(
        chunk_id=chunk_id,
        document_id=DOCUMENT,
        content=f"the text of chunk {index}",
        chunk_index=index,
        section="3.1 Evaluation",
        page_start=2,
        page_end=2,
        embedding_model_id="stub-embedder/v1",
    )


class RecordingVectorStore(InMemoryVectorStore):
    """The in-memory store, remembering how it was called."""

    def __init__(self, matches: tuple[VectorMatch, ...] = ()) -> None:
        super().__init__()
        self.matches = matches
        self.calls: list[dict[str, object]] = []
        self.fail_search = False

    async def search(self, query_vector, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"vector": query_vector, **kwargs})
        if self.fail_search:
            raise VectorStoreError("could not search")
        return self.matches


class FakeChunkRepository:
    """Stands in for the database half, which integration tests cover for real."""

    def __init__(self, validated: dict[str, ValidatedChunk] | None = None) -> None:
        self.validated = validated or {}
        self.calls: list[dict[str, object]] = []
        self.explode = False

    async def validate_for_retrieval(self, chunk_ids, user_id, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"chunk_ids": list(chunk_ids), "user_id": user_id, **kwargs})
        if self.explode:
            raise RuntimeError("connection reset by peer at db.internal:5432")
        return {k: v for k, v in self.validated.items() if k in set(chunk_ids)}


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _service(
    *,
    store: RecordingVectorStore | None = None,
    repository: FakeChunkRepository | None = None,
    embedder: StubEmbeddingProvider | None = None,
    top_k: int = 10,
    score_threshold: float = 0.7,
    max_query_chars: int = 2000,
    session: FakeSession | None = None,
) -> SearchService:
    service = SearchService(
        session or FakeSession(),  # type: ignore[arg-type]
        embedder=embedder or StubEmbeddingProvider(),
        vectors=store or RecordingVectorStore(),
        top_k=top_k,
        score_threshold=score_threshold,
        max_query_chars=max_query_chars,
    )
    service._chunks = repository or FakeChunkRepository()  # type: ignore[assignment]
    return service


def _match(chunk_id: str, score: float, owner: str = ALICE.user_id) -> VectorMatch:
    return VectorMatch(
        id=chunk_id,
        score=score,
        payload=VectorPayload(
            user_id=owner,
            document_id=DOCUMENT,
            chunk_id=chunk_id,
            chunk_index=0,
            section=None,
            page_start=1,
            page_end=1,
            strategy_version="section-aware/v1",
            tokenizer_id="regex-word/v1",
            embedding_model_id="stub-embedder/v1",
            dimension=STUB_DIMENSION,
        ),
    )


class TestQueryValidation:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t", " ", "　"])
    async def test_an_empty_query_is_refused(self, text: str) -> None:
        """Including the whitespace nobody thinks of as whitespace."""
        with pytest.raises(InvalidSearchQuery):
            await _service().search(RetrievalQuery(text=text), ALICE)

    async def test_an_empty_query_never_reaches_the_index(self) -> None:
        store = RecordingVectorStore()
        with pytest.raises(InvalidSearchQuery):
            await _service(store=store).search(RetrievalQuery(text="  "), ALICE)
        assert store.calls == []

    async def test_a_query_at_the_character_limit_is_accepted(self) -> None:
        """Exactly at it, measured after stripping — the boundary is inclusive."""
        text = "x" * 50
        assert len(text) == 50
        await _service(max_query_chars=50).search(RetrievalQuery(text=text), ALICE)

    async def test_one_character_over_the_limit_is_refused(self) -> None:
        with pytest.raises(InvalidSearchQuery) as raised:
            await _service(max_query_chars=10).search(
                RetrievalQuery(text="x" * 11), ALICE
            )
        assert "10 characters" in str(raised.value)

    async def test_a_query_longer_than_the_model_reads_is_refused(self) -> None:
        """The real limit. Past it the model truncates and the vector
        describes a question nobody asked (ADR-0014 §8)."""
        embedder = StubEmbeddingProvider(max_input_tokens=12)
        with pytest.raises(InvalidSearchQuery) as raised:
            await _service(embedder=embedder, max_query_chars=10_000).search(
                RetrievalQuery(text="word " * 50), ALICE
            )
        assert str(12 - SPECIAL_TOKENS_PER_SEQUENCE) in str(raised.value)

    async def test_a_query_that_exactly_fills_the_model_is_accepted(self) -> None:
        embedder = StubEmbeddingProvider(max_input_tokens=12)
        service = _service(embedder=embedder, max_query_chars=10_000)
        await service.search(RetrievalQuery(text="word " * 10), ALICE)

    async def test_the_query_is_normalised_before_it_is_embedded(self) -> None:
        """NFC, the form `pdf_parser.py` leaves the corpus in, so the query and
        the documents are compared in one normal form rather than two."""
        embedder = StubEmbeddingProvider()
        service = _service(embedder=embedder)
        await service.search(RetrievalQuery(text="café study"), ALICE)
        await service.search(RetrievalQuery(text="café study"), ALICE)
        assert embedder.queries[0] == embedder.queries[1] == "café study"

    async def test_surrounding_whitespace_is_stripped(self) -> None:
        embedder = StubEmbeddingProvider()
        await _service(embedder=embedder).search(
            RetrievalQuery(text="  retrieval quality \n"), ALICE
        )
        assert embedder.queries == ["retrieval quality"]

    async def test_the_refusal_never_contains_the_query(self) -> None:
        """These messages reach logs and, one day, an HTTP body."""
        secret = "my unpublished hypothesis about " + "x" * 200
        with pytest.raises(InvalidSearchQuery) as raised:
            await _service(max_query_chars=20).search(
                RetrievalQuery(text=secret), ALICE
            )
        assert "hypothesis" not in str(raised.value)


class TestTheOwnerComesFromThePrincipal:
    async def test_the_index_is_asked_for_the_principals_own_chunks(self) -> None:
        store = RecordingVectorStore()
        await _service(store=store).search(RetrievalQuery(text="q"), ALICE)
        assert store.calls[0]["owner_id"] == ALICE.user_id

    async def test_a_different_principal_gets_a_different_scope(self) -> None:
        store = RecordingVectorStore()
        service = _service(store=store)
        await service.search(RetrievalQuery(text="q"), ALICE)
        await service.search(RetrievalQuery(text="q"), BOB)
        assert [call["owner_id"] for call in store.calls] == [
            ALICE.user_id,
            BOB.user_id,
        ]

    async def test_the_query_model_has_nowhere_to_put_an_owner(self) -> None:
        """Not "a forged owner is rejected" — there is no field to forge."""
        assert "user_id" not in RetrievalQuery.model_fields
        assert "owner_id" not in RetrievalQuery.model_fields
        with pytest.raises(Exception):
            RetrievalQuery(text="q", owner_id=BOB.user_id)  # type: ignore[call-arg]

    async def test_the_database_is_asked_with_the_principals_id_too(self) -> None:
        """Both layers get the same owner, from the same place."""
        repository = FakeChunkRepository({"c1": _chunk("c1")})
        store = RecordingVectorStore((_match("c1", 0.9),))
        await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert repository.calls[0]["user_id"] == ALICE.user_id

    async def test_a_payload_claiming_another_owner_changes_nothing(self) -> None:
        """The payload is not read. This asserts it by making it a lie."""
        repository = FakeChunkRepository({"c1": _chunk("c1")})
        store = RecordingVectorStore((_match("c1", 0.9, owner=BOB.user_id),))
        results = await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert repository.calls[0]["user_id"] == ALICE.user_id
        assert [r.chunk_id for r in results] == ["c1"]


class TestTheEmbeddingFlow:
    async def test_the_query_is_embedded_before_the_index_is_searched(self) -> None:
        embedder = StubEmbeddingProvider()
        store = RecordingVectorStore()
        await _service(embedder=embedder, store=store).search(
            RetrievalQuery(text="retrieval quality"), ALICE
        )
        assert embedder.queries == ["retrieval quality"]
        assert store.calls[0]["vector"] == await embedder.embed_query(
            "retrieval quality"
        )

    async def test_the_validation_asks_for_this_model_only(self) -> None:
        """ADR-0014 §4: a chunk from another model is in another space."""
        embedder = StubEmbeddingProvider(model_id="model/v2", dimension=8)
        repository = FakeChunkRepository({"c1": _chunk("c1")})
        store = RecordingVectorStore((_match("c1", 0.9),))
        await _service(embedder=embedder, store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert repository.calls[0]["embedding_model_id"] == "model/v2"
        assert repository.calls[0]["dimension"] == 8


class TestTopKAndThreshold:
    async def test_the_configured_defaults_are_used(self) -> None:
        store = RecordingVectorStore()
        await _service(store=store, top_k=7, score_threshold=0.55).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert store.calls[0]["limit"] == 7
        assert store.calls[0]["score_threshold"] == 0.55

    async def test_the_query_may_narrow_them(self) -> None:
        store = RecordingVectorStore()
        await _service(store=store, top_k=7, score_threshold=0.55).search(
            RetrievalQuery(text="q", top_k=3, score_threshold=0.9), ALICE
        )
        assert store.calls[0]["limit"] == 3
        assert store.calls[0]["score_threshold"] == 0.9

    async def test_a_zero_threshold_is_honoured_not_treated_as_absent(self) -> None:
        """`0.0 or default` would silently restore the default."""
        store = RecordingVectorStore()
        await _service(store=store, score_threshold=0.7).search(
            RetrievalQuery(text="q", score_threshold=0.0), ALICE
        )
        assert store.calls[0]["score_threshold"] == 0.0

    @pytest.mark.parametrize("top_k", [0, -1])
    async def test_a_nonsense_top_k_is_refused_by_the_model(self, top_k: int) -> None:
        with pytest.raises(Exception):
            RetrievalQuery(text="q", top_k=top_k)

    @pytest.mark.parametrize("threshold", [-0.1, 1.1])
    async def test_a_threshold_outside_the_range_is_refused(
        self, threshold: float
    ) -> None:
        with pytest.raises(Exception):
            RetrievalQuery(text="q", score_threshold=threshold)

    async def test_document_ids_are_passed_through_to_narrow_the_search(self) -> None:
        store = RecordingVectorStore()
        await _service(store=store).search(
            RetrievalQuery(text="q", document_ids=(DOCUMENT,)), ALICE
        )
        assert store.calls[0]["document_ids"] == [DOCUMENT]

    async def test_no_document_ids_means_the_whole_corpus(self) -> None:
        store = RecordingVectorStore()
        await _service(store=store).search(RetrievalQuery(text="q"), ALICE)
        assert store.calls[0]["document_ids"] is None


class TestOrderingAndDeduplication:
    async def test_results_keep_the_indexs_order(self) -> None:
        store = RecordingVectorStore(
            (_match("a", 0.91), _match("b", 0.87), _match("c", 0.82))
        )
        repository = FakeChunkRepository(
            {"a": _chunk("a", 5), "b": _chunk("b", 1), "c": _chunk("c", 3)}
        )
        results = await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert [r.chunk_id for r in results] == ["a", "b", "c"]
        assert [r.score for r in results] == [0.91, 0.87, 0.82]

    async def test_a_dropped_candidate_does_not_reorder_the_survivors(self) -> None:
        """The example in the brief: B fails validation, A and C keep their order."""
        store = RecordingVectorStore(
            (_match("a", 0.91), _match("b", 0.87), _match("c", 0.82))
        )
        repository = FakeChunkRepository({"a": _chunk("a", 5), "c": _chunk("c", 3)})
        results = await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert [(r.chunk_id, r.score) for r in results] == [("a", 0.91), ("c", 0.82)]

    async def test_the_databases_row_order_is_not_used(self) -> None:
        """The fake returns its dict in an order that contradicts the scores."""
        store = RecordingVectorStore((_match("z", 0.95), _match("a", 0.60)))
        repository = FakeChunkRepository({"a": _chunk("a", 0), "z": _chunk("z", 9)})
        results = await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert [r.chunk_id for r in results] == ["z", "a"]

    async def test_a_duplicate_candidate_yields_one_result(self) -> None:
        store = RecordingVectorStore(
            (_match("a", 0.91), _match("a", 0.70), _match("b", 0.60))
        )
        repository = FakeChunkRepository({"a": _chunk("a"), "b": _chunk("b", 1)})
        results = await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert [(r.chunk_id, r.score) for r in results] == [("a", 0.91), ("b", 0.60)]

    async def test_a_duplicate_does_not_consume_two_slots_of_the_batch(self) -> None:
        store = RecordingVectorStore((_match("a", 0.91), _match("a", 0.70)))
        repository = FakeChunkRepository({"a": _chunk("a")})
        await _service(store=store, repository=repository).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert repository.calls[0]["chunk_ids"] == ["a"]


class TestEmptyResults:
    async def test_an_index_with_nothing_to_offer_returns_nothing(self) -> None:
        results = await _service().search(RetrievalQuery(text="q"), ALICE)
        assert results == ()

    async def test_no_candidates_means_the_database_is_not_asked(self) -> None:
        repository = FakeChunkRepository()
        await _service(repository=repository).search(RetrievalQuery(text="q"), ALICE)
        assert repository.calls == []

    async def test_every_candidate_dropped_returns_nothing_not_an_error(self) -> None:
        """Stale vectors are ordinary (ADR-0014 §3): they degrade to fewer
        results, and to none when every candidate is stale."""
        store = RecordingVectorStore((_match("gone", 0.99),))
        results = await _service(store=store, repository=FakeChunkRepository()).search(
            RetrievalQuery(text="q"), ALICE
        )
        assert results == ()


class TestFailuresAreNotEmptyResults:
    async def test_an_unreachable_index_is_an_error(self) -> None:
        store = RecordingVectorStore()
        store.fail_search = True
        with pytest.raises(SearchUnavailable):
            await _service(store=store).search(RetrievalQuery(text="q"), ALICE)

    async def test_a_failed_embedding_is_an_error(self) -> None:
        class Failing(StubEmbeddingProvider):
            async def embed_query(self, text: str):  # type: ignore[no-untyped-def]
                raise EmbeddingError("onnx session died at /home/x/.cache")

        with pytest.raises(SearchUnavailable) as raised:
            await _service(embedder=Failing()).search(RetrievalQuery(text="q"), ALICE)
        assert "onnx" not in str(raised.value)

    async def test_a_failed_validation_is_an_error(self) -> None:
        repository = FakeChunkRepository({"a": _chunk("a")})
        repository.explode = True
        store = RecordingVectorStore((_match("a", 0.9),))
        with pytest.raises(SearchUnavailable) as raised:
            await _service(store=store, repository=repository).search(
                RetrievalQuery(text="q"), ALICE
            )
        assert "5432" not in str(raised.value)
        assert "db.internal" not in str(raised.value)

    async def test_no_dependency_exception_reaches_the_traceback(self) -> None:
        """These become a log line and, in M5, something a user sees."""
        repository = FakeChunkRepository({"a": _chunk("a")})
        repository.explode = True
        store = RecordingVectorStore((_match("a", 0.9),))
        with pytest.raises(SearchUnavailable) as raised:
            await _service(store=store, repository=repository).search(
                RetrievalQuery(text="q"), ALICE
            )
        rendered = "".join(
            __import__("traceback").format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert "connection reset" not in rendered
        assert "RuntimeError" not in rendered

    async def test_an_invalid_query_is_not_an_availability_problem(self) -> None:
        """Different types, because the caller's remedy is different."""
        with pytest.raises(InvalidSearchQuery) as raised:
            await _service().search(RetrievalQuery(text=" "), ALICE)
        assert not isinstance(raised.value, SearchUnavailable)


class TestTransactionBoundaries:
    async def test_the_read_transaction_is_closed_before_returning(self) -> None:
        session = FakeSession()
        store = RecordingVectorStore((_match("a", 0.9),))
        await _service(
            session=session,
            store=store,
            repository=FakeChunkRepository({"a": _chunk("a")}),
        ).search(RetrievalQuery(text="q"), ALICE)
        assert session.rollbacks == 1

    async def test_it_is_closed_even_when_validation_fails(self) -> None:
        session = FakeSession()
        repository = FakeChunkRepository({"a": _chunk("a")})
        repository.explode = True
        store = RecordingVectorStore((_match("a", 0.9),))
        with pytest.raises(SearchUnavailable):
            await _service(session=session, store=store, repository=repository).search(
                RetrievalQuery(text="q"), ALICE
            )
        assert session.rollbacks == 1

    async def test_nothing_is_opened_when_there_are_no_candidates(self) -> None:
        session = FakeSession()
        await _service(session=session).search(RetrievalQuery(text="q"), ALICE)
        assert session.rollbacks == 0
