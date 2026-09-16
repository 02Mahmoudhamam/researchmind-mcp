"""The Qdrant vector store, against a real Qdrant (M3/S3.5).

Marked `qdrant`: nothing here is mocked. A mocked Qdrant would prove that the
mock filters by owner, which is not the claim. The claim is that **the server**
does — ADR-0003 §5's isolation invariant — and the only way to assert it is to
ask a real one for another tenant's vectors and get nothing back.

Each test gets its own collection (`vector_store` fixture) and drops it.
"""

import uuid

import pytest
from qdrant_client import models

from shared.interfaces.vector_store import (
    VectorPayload,
    VectorRecord,
    VectorStoreError,
)
from vector_db.qdrant.repository import QdrantVectorStore

pytestmark = pytest.mark.qdrant

DIMENSION = 8
ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())


def _vector(seed: float) -> tuple[float, ...]:
    """A unit vector, deterministic in `seed`, so scores are predictable."""
    raw = [seed] + [1.0] * (DIMENSION - 1)
    length = sum(value * value for value in raw) ** 0.5
    return tuple(value / length for value in raw)


def _record(
    *,
    owner: str,
    document_id: str,
    index: int = 0,
    seed: float = 1.0,
    chunk_id: str | None = None,
) -> VectorRecord:
    chunk = chunk_id or str(uuid.uuid4())
    return VectorRecord(
        id=chunk,
        vector=_vector(seed),
        payload=VectorPayload(
            user_id=owner,
            document_id=document_id,
            chunk_id=chunk,
            chunk_index=index,
            section="3.1 Evaluation",
            page_start=2,
            page_end=3,
            strategy_version="section-aware/v1",
            tokenizer_id="bge-small-en-v1.5/wordpiece",
            embedding_model_id="BAAI/bge-small-en-v1.5",
            dimension=DIMENSION,
        ),
    )


class TestTheCollection:
    async def test_it_is_created_at_the_providers_dimension(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """Not a literal: the scaffold's hard-coded 1536 is what this replaces."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        info = await vector_store._client.get_collection(vector_store._collection)
        assert info.config.params.vectors.size == DIMENSION
        assert info.config.params.vectors.distance == models.Distance.COSINE

    async def test_creating_it_twice_is_harmless(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """Startup runs on every worker boot, and there may be several."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        await vector_store.ensure_collection(dimension=DIMENSION)
        assert await vector_store._client.collection_exists(vector_store._collection)

    async def test_a_collection_of_another_width_is_refused(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """A model change that nobody dealt with. Searching it would be wrong."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        with pytest.raises(VectorStoreError) as raised:
            await vector_store.ensure_collection(dimension=DIMENSION + 1)
        assert str(DIMENSION) in str(raised.value)

    async def test_the_owner_field_is_indexed(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """Every query filters on it; without an index each one is a scan."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        info = await vector_store._client.get_collection(vector_store._collection)
        assert {"user_id", "document_id"} <= set(info.payload_schema)

    @pytest.mark.parametrize("dimension", [0, -1])
    async def test_a_nonsense_dimension_is_refused(
        self, vector_store: QdrantVectorStore, dimension: int
    ) -> None:
        with pytest.raises(VectorStoreError):
            await vector_store.ensure_collection(dimension=dimension)


class TestWriting:
    async def test_vectors_are_stored_with_their_payload(
        self, vector_store: QdrantVectorStore
    ) -> None:
        await vector_store.ensure_collection(dimension=DIMENSION)
        document = str(uuid.uuid4())
        record = _record(owner=ALICE, document_id=document)
        await vector_store.upsert([record])

        found = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=5, score_threshold=0.0
        )
        assert [match.payload for match in found] == [record.payload]

    async def test_the_payload_carries_no_chunk_text(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """ADR-0013 §5. There is no field for it, and this asserts it stays so."""
        stored = _record(owner=ALICE, document_id=str(uuid.uuid4()))
        keys = set(stored.payload.as_mapping())
        assert not keys & {"content", "text", "chunk_text", "principal", "token"}
        assert keys == {
            "user_id",
            "document_id",
            "chunk_id",
            "chunk_index",
            "section",
            "page_start",
            "page_end",
            "strategy_version",
            "tokenizer_id",
            "embedding_model_id",
            "dimension",
        }

    async def test_writing_the_same_chunk_twice_leaves_one_vector(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """ADR-0013 §4: the point id is the chunk id, so a retry overwrites.

        This is what makes a redelivery, a restart or a partial failure
        converge instead of accumulating a second copy of every vector.
        """
        await vector_store.ensure_collection(dimension=DIMENSION)
        document = str(uuid.uuid4())
        chunk_id = str(uuid.uuid4())
        await vector_store.upsert(
            [_record(owner=ALICE, document_id=document, chunk_id=chunk_id, seed=1.0)]
        )
        await vector_store.upsert(
            [_record(owner=ALICE, document_id=document, chunk_id=chunk_id, seed=9.0)]
        )
        assert (
            await vector_store.count_for_document(document_id=document, owner_id=ALICE)
            == 1
        )

    async def test_writing_nothing_is_not_an_error(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """A document with no chunks must not need a special case at the caller."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        await vector_store.upsert([])

    async def test_a_vector_of_the_wrong_width_is_refused(
        self, vector_store: QdrantVectorStore
    ) -> None:
        await vector_store.ensure_collection(dimension=DIMENSION)
        wrong = VectorRecord(
            id=str(uuid.uuid4()),
            vector=(0.1, 0.2),
            payload=_record(owner=ALICE, document_id=str(uuid.uuid4())).payload,
        )
        with pytest.raises(VectorStoreError):
            await vector_store.upsert([wrong])

    async def test_writing_to_a_missing_collection_fails_loudly(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """`ready` is set on the strength of this call. It must not pass quietly."""
        with pytest.raises(VectorStoreError) as raised:
            await vector_store.upsert(
                [_record(owner=ALICE, document_id=str(uuid.uuid4()))]
            )
        assert "could not write" in str(raised.value)


class TestTenantIsolation:
    """ADR-0003 §5. The invariant the whole design exists to keep."""

    @pytest.fixture()
    async def two_tenants(self, vector_store: QdrantVectorStore) -> tuple[str, str]:
        await vector_store.ensure_collection(dimension=DIMENSION)
        alice_document, bob_document = str(uuid.uuid4()), str(uuid.uuid4())
        await vector_store.upsert(
            [
                _record(owner=ALICE, document_id=alice_document, index=0, seed=1.0),
                _record(owner=ALICE, document_id=alice_document, index=1, seed=1.1),
                _record(owner=BOB, document_id=bob_document, index=0, seed=1.0),
            ]
        )
        return alice_document, bob_document

    async def test_a_search_returns_only_the_searchers_own_vectors(
        self, vector_store: QdrantVectorStore, two_tenants: tuple[str, str]
    ) -> None:
        alice_document, _ = two_tenants
        found = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=10, score_threshold=0.0
        )
        assert len(found) == 2
        assert {match.payload.user_id for match in found} == {ALICE}
        assert {match.payload.document_id for match in found} == {alice_document}

    async def test_one_tenant_cannot_reach_anothers_vectors(
        self, vector_store: QdrantVectorStore, two_tenants: tuple[str, str]
    ) -> None:
        """Bob searches the vector Alice's chunks are nearest to, and gets his own."""
        _, bob_document = two_tenants
        found = await vector_store.search(
            _vector(1.0), owner_id=BOB, limit=10, score_threshold=0.0
        )
        assert {match.payload.document_id for match in found} == {bob_document}

    async def test_naming_another_tenants_document_returns_nothing(
        self, vector_store: QdrantVectorStore, two_tenants: tuple[str, str]
    ) -> None:
        """A forged document id. The owner condition is `must`, so it still holds."""
        alice_document, _ = two_tenants
        found = await vector_store.search(
            _vector(1.0),
            owner_id=BOB,
            limit=10,
            score_threshold=0.0,
            document_ids=[alice_document],
        )
        assert found == ()

    async def test_a_forged_user_id_matches_nothing(
        self, vector_store: QdrantVectorStore, two_tenants: tuple[str, str]
    ) -> None:
        found = await vector_store.search(
            _vector(1.0), owner_id=str(uuid.uuid4()), limit=10, score_threshold=0.0
        )
        assert found == ()

    async def test_the_owner_condition_is_in_the_query_the_server_runs(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """Not post-filtering: post-filtering means the rows were already read."""
        built = vector_store._owned_by(ALICE)
        assert built.should is None
        assert [(condition.key, condition.match.value) for condition in built.must] == [
            ("user_id", ALICE)
        ]

    async def test_document_scoping_narrows_and_never_widens(
        self, vector_store: QdrantVectorStore
    ) -> None:
        built = vector_store._owned_by(ALICE, ["one", "two"])
        assert [condition.key for condition in built.must] == [
            "user_id",
            "document_id",
        ]
        assert built.should is None

    @pytest.mark.parametrize("empty", ["", None])
    async def test_a_search_without_an_owner_is_refused(
        self, vector_store: QdrantVectorStore, empty: str | None
    ) -> None:
        """There is no such thing as an unscoped query on this path."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        with pytest.raises(VectorStoreError):
            await vector_store.search(
                _vector(1.0),
                owner_id=empty,  # type: ignore[arg-type]
                limit=10,
                score_threshold=0.0,
            )

    async def test_counting_is_owner_scoped_too(
        self, vector_store: QdrantVectorStore, two_tenants: tuple[str, str]
    ) -> None:
        alice_document, _ = two_tenants
        assert (
            await vector_store.count_for_document(
                document_id=alice_document, owner_id=ALICE
            )
            == 2
        )
        assert (
            await vector_store.count_for_document(
                document_id=alice_document, owner_id=BOB
            )
            == 0
        )


class TestSearching:
    @pytest.fixture()
    async def corpus(self, vector_store: QdrantVectorStore) -> str:
        await vector_store.ensure_collection(dimension=DIMENSION)
        document = str(uuid.uuid4())
        await vector_store.upsert(
            [
                _record(owner=ALICE, document_id=document, index=i, seed=1.0 + i)
                for i in range(6)
            ]
        )
        return document

    async def test_results_come_back_best_first(
        self, vector_store: QdrantVectorStore, corpus: str
    ) -> None:
        found = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=6, score_threshold=0.0
        )
        assert [match.score for match in found] == sorted(
            (match.score for match in found), reverse=True
        )

    async def test_top_k_limits_what_comes_back(
        self, vector_store: QdrantVectorStore, corpus: str
    ) -> None:
        found = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=2, score_threshold=0.0
        )
        assert len(found) == 2

    async def test_the_score_threshold_excludes_weak_matches(
        self, vector_store: QdrantVectorStore, corpus: str
    ) -> None:
        loose = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=10, score_threshold=0.0
        )
        strict = await vector_store.search(
            _vector(1.0), owner_id=ALICE, limit=10, score_threshold=0.999
        )
        assert len(strict) < len(loose)
        assert all(match.score >= 0.999 for match in strict)

    async def test_an_empty_collection_returns_nothing(
        self, vector_store: QdrantVectorStore
    ) -> None:
        await vector_store.ensure_collection(dimension=DIMENSION)
        assert (
            await vector_store.search(
                _vector(1.0), owner_id=ALICE, limit=10, score_threshold=0.0
            )
            == ()
        )

    @pytest.mark.parametrize("limit", [0, -1])
    async def test_a_nonsense_limit_is_refused(
        self, vector_store: QdrantVectorStore, limit: int
    ) -> None:
        with pytest.raises(VectorStoreError):
            await vector_store.search(
                _vector(1.0), owner_id=ALICE, limit=limit, score_threshold=0.0
            )


class TestDeleting:
    async def test_it_removes_one_documents_vectors_and_leaves_the_rest(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """Re-chunking deletes before it writes, so this must be exact."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        doomed, kept = str(uuid.uuid4()), str(uuid.uuid4())
        await vector_store.upsert(
            [
                _record(owner=ALICE, document_id=doomed),
                _record(owner=ALICE, document_id=kept),
            ]
        )
        await vector_store.delete_document(document_id=doomed, owner_id=ALICE)
        assert (
            await vector_store.count_for_document(document_id=doomed, owner_id=ALICE)
            == 0
        )
        assert (
            await vector_store.count_for_document(document_id=kept, owner_id=ALICE) == 1
        )

    async def test_it_cannot_delete_another_tenants_document(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """A document id is unique in PostgreSQL. This is a different store."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        document = str(uuid.uuid4())
        await vector_store.upsert([_record(owner=ALICE, document_id=document)])
        await vector_store.delete_document(document_id=document, owner_id=BOB)
        assert (
            await vector_store.count_for_document(document_id=document, owner_id=ALICE)
            == 1
        )

    async def test_deleting_nothing_is_not_an_error(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """The recovery path deletes before it knows whether anything is there."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        await vector_store.delete_document(
            document_id=str(uuid.uuid4()), owner_id=ALICE
        )


class TestFailuresLeakNothing:
    async def test_a_qdrant_error_does_not_reach_the_traceback(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """These messages are written to `failure_reason` and to the logs.

        Qdrant's own errors render the URL, the headers and the response body —
        and the body can contain the payload that was being written.
        """
        record = _record(owner=ALICE, document_id=str(uuid.uuid4()))
        with pytest.raises(VectorStoreError) as raised:
            await vector_store.upsert([record])

        rendered = "".join(
            __import__("traceback").format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert "UnexpectedResponse" not in rendered
        assert ALICE not in str(raised.value)
        assert vector_store._config.host not in str(raised.value)

    async def test_a_malformed_payload_is_not_accepted_as_a_citation(
        self, vector_store: QdrantVectorStore
    ) -> None:
        """A point written by something else, or by an older schema."""
        await vector_store.ensure_collection(dimension=DIMENSION)
        await vector_store._client.upsert(
            collection_name=vector_store._collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=list(_vector(1.0)),
                    payload={"user_id": ALICE, "document_id": "d"},
                )
            ],
            wait=True,
        )
        with pytest.raises(VectorStoreError) as raised:
            await vector_store.search(
                _vector(1.0), owner_id=ALICE, limit=5, score_threshold=0.0
            )
        assert "malformed" in str(raised.value)
