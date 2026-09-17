"""Test doubles for the embedding seam — M3/S3.5.

The state-machine, recovery and parsing tests are not about embeddings, and
loading a 67 MB model and reaching a Qdrant for each of them would make them
slow and make their failures ambiguous. They get these instead, exactly as they
already get `_AcceptingIngestionQueue` in place of Redis.

Both are **real implementations of the protocols**, not mocks: the store
filters by owner, the provider refuses empty text and returns vectors of the
width it declares. Tests that are about embeddings use the real model and a
real Qdrant (`tests/unit/test_embeddings.py`,
`tests/integration/test_vector_store.py`, `tests/integration/test_embed_ingestion.py`).
"""

import hashlib
import math
from typing import Sequence

from document_processing.tokenization import RegexTokenizer
from shared.interfaces.embedding import EmbeddingError, Vector
from shared.interfaces.tokenization import Tokenizer
from shared.interfaces.vector_store import (
    VectorMatch,
    VectorPayload,
    VectorRecord,
    VectorStoreError,
)

STUB_MODEL_ID = "stub-embedder/v1"
STUB_DIMENSION = 8


class StubEmbeddingProvider:
    """An `EmbeddingProvider` that hashes text instead of embedding it.

    Deterministic and unit-length, which is all the pipeline requires of a
    vector. It counts with the provisional `regex-word/v1` tokenizer, so tests
    using it see no version mismatch unless they arrange one.
    """

    def __init__(
        self,
        *,
        model_id: str = STUB_MODEL_ID,
        dimension: int = STUB_DIMENSION,
        max_input_tokens: int = 4096,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self.model_id = model_id
        self.dimension = dimension
        self.max_input_tokens = max_input_tokens
        self._tokenizer = tokenizer or RegexTokenizer()
        self.embedded: list[tuple[str, ...]] = []
        # What `embed_query` was handed, so a test can assert the query was
        # normalised and stripped before it reached the model.
        self.queries: list[str] = []

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tokenizer

    async def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        if not texts:
            return ()
        self._refuse_empty(texts)
        self.embedded.append(tuple(texts))
        return tuple(self._vector(text) for text in texts)

    async def embed_query(self, text: str) -> Vector:
        self._refuse_empty([text])
        self.queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> Vector:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 + 0.1 for i in range(self.dimension)]
        length = math.sqrt(sum(value * value for value in raw))
        return tuple(value / length for value in raw)

    @staticmethod
    def _refuse_empty(texts: Sequence[str]) -> None:
        if any(not text or not text.strip() for text in texts):
            raise EmbeddingError("a text to embed was empty")


class FailingEmbeddingProvider(StubEmbeddingProvider):
    """Fails every call, to exercise the transient path."""

    async def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        raise EmbeddingError("the embedding model failed")


class InMemoryVectorStore:
    """A `VectorStore` in a dictionary. Owner-scoped, like the real one.

    The scoping is not decoration: a double that returned everything would let
    a test pass while the pipeline handed one tenant another's vectors. The
    isolation *invariant* is still asserted against a real Qdrant — this only
    keeps the double from lying.
    """

    def __init__(self) -> None:
        self.points: dict[str, VectorRecord] = {}
        self.dimension: int | None = None
        self.upserts = 0
        self.deletes: list[tuple[str, str]] = []
        self.fail_upsert = False
        self.fail_delete = False

    async def ensure_collection(self, *, dimension: int) -> None:
        if dimension <= 0:
            raise VectorStoreError("a collection needs a positive dimension")
        if self.dimension is not None and self.dimension != dimension:
            raise VectorStoreError("the collection holds vectors of another width")
        self.dimension = dimension

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        if self.fail_upsert:
            raise VectorStoreError("could not write vectors")
        for record in records:
            if self.dimension is not None and len(record.vector) != self.dimension:
                raise VectorStoreError("a vector is not the collection's width")
            self.points[record.id] = record
        self.upserts += 1

    async def search(
        self,
        query_vector: Vector,
        *,
        owner_id: str,
        limit: int,
        score_threshold: float,
        document_ids: Sequence[str] | None = None,
    ) -> tuple[VectorMatch, ...]:
        if not owner_id:
            raise VectorStoreError("a query without an owner is not a query")
        if limit <= 0:
            raise VectorStoreError("a search needs a positive limit")
        scored = [
            VectorMatch(
                id=record.id,
                score=sum(a * b for a, b in zip(query_vector, record.vector)),
                payload=record.payload,
            )
            for record in self.points.values()
            if record.payload.user_id == owner_id
            and (document_ids is None or record.payload.document_id in document_ids)
        ]
        kept = [match for match in scored if match.score >= score_threshold]
        kept.sort(key=lambda match: match.score, reverse=True)
        return tuple(kept[:limit])

    async def delete_document(self, *, document_id: str, owner_id: str) -> None:
        if self.fail_delete:
            raise VectorStoreError("could not delete vectors")
        self.deletes.append((document_id, owner_id))
        self.points = {
            point_id: record
            for point_id, record in self.points.items()
            if not (
                record.payload.document_id == document_id
                and record.payload.user_id == owner_id
            )
        }

    async def count_for_document(self, *, document_id: str, owner_id: str) -> int:
        return sum(
            1
            for record in self.points.values()
            if record.payload.document_id == document_id
            and record.payload.user_id == owner_id
        )

    def payloads_for(self, owner_id: str) -> list[VectorPayload]:
        """Every payload this owner has, in chunk order. For assertions."""
        return sorted(
            (
                record.payload
                for record in self.points.values()
                if record.payload.user_id == owner_id
            ),
            key=lambda payload: payload.chunk_index,
        )
