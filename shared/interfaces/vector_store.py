"""Storing and searching vectors — the seam between the application and Qdrant.

A `Protocol`, not the scaffold's ABC, and the difference is the point.
`BaseVectorStore.search(..., filters: Optional[dict] = None)` made the owner
filter **optional**, so the unsafe call was the short one and forgetting a
`filters` dict returned every tenant's chunks. `docs/security/principles.md` §3
requires the opposite: the ownership filter is "a required parameter of the
repository signature", and "the safe path must be the only path". Here
`owner_id` is keyword-required on every operation that can reach a stored
vector, and ADR-0013 §5 puts it inside the query the server runs.

The payload is a **typed record**, not a free dictionary. ADR-0003 §4 lists what
a citation needs; nothing else may be stored, and there is deliberately no
field for chunk text, a credential or a `Principal`, so leaking one is not
something a caller can do by accident.
"""

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from shared.interfaces.embedding import Vector


class VectorStoreError(Exception):
    """The vector store could not do what was asked.

    Raised instead of a vendor exception, so the message that reaches
    `failure_reason` and the logs carries no host, credential or chunk text.
    """


@dataclass(frozen=True)
class VectorPayload:
    """What a vector carries alongside itself (ADR-0003 §4, ADR-0013 §5).

    Denormalised on purpose: retrieval must be able to render a citation —
    which document, which section, which pages — without a second round trip,
    and must be able to tell which model and strategy produced the vector it
    just matched.
    """

    user_id: str
    document_id: str
    chunk_id: str
    chunk_index: int
    section: str | None
    page_start: int
    page_end: int
    strategy_version: str
    tokenizer_id: str
    embedding_model_id: str
    dimension: int

    def as_mapping(self) -> dict[str, object]:
        """The payload as the store writes it. No text, no credentials."""
        return {
            "user_id": self.user_id,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "section": self.section,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "strategy_version": self.strategy_version,
            "tokenizer_id": self.tokenizer_id,
            "embedding_model_id": self.embedding_model_id,
            "dimension": self.dimension,
        }

    @staticmethod
    def _text(payload: Mapping[str, object], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str):
            raise TypeError(f"{key} is {type(value).__name__}, not a string")
        return value

    @staticmethod
    def _whole(payload: Mapping[str, object], key: str) -> int:
        """An integer, not something that merely converts to one.

        `int("7")` succeeds, and a payload holding "7" where 7 belongs came
        from something other than this store.
        """
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{key} is {type(value).__name__}, not an integer")
        return value

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "VectorPayload":
        """Rebuild a payload a store returned.

        :raises VectorStoreError: if a field is missing or the wrong shape —
            a point written by something else, or by an older schema, is not
            quietly accepted as a citation.
        """
        try:
            section = payload["section"]
            return cls(
                user_id=cls._text(payload, "user_id"),
                document_id=cls._text(payload, "document_id"),
                chunk_id=cls._text(payload, "chunk_id"),
                chunk_index=cls._whole(payload, "chunk_index"),
                section=None if section is None else cls._text(payload, "section"),
                page_start=cls._whole(payload, "page_start"),
                page_end=cls._whole(payload, "page_end"),
                strategy_version=cls._text(payload, "strategy_version"),
                tokenizer_id=cls._text(payload, "tokenizer_id"),
                embedding_model_id=cls._text(payload, "embedding_model_id"),
                dimension=cls._whole(payload, "dimension"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorStoreError(
                f"a stored vector's payload is missing or malformed: {exc}"
            ) from None


@dataclass(frozen=True)
class VectorRecord:
    """One vector as it is written: its chunk's id, its numbers, its payload."""

    # ADR-0013 §4: a chunk's id *is* its point id, and it is derived, so
    # writing the same chunk twice overwrites one point instead of making two.
    id: str
    vector: Vector
    payload: VectorPayload


@dataclass(frozen=True)
class VectorMatch:
    """One search hit: what matched, and how well."""

    id: str
    score: float
    payload: VectorPayload


class VectorStore(Protocol):
    """Vector persistence and owner-scoped similarity search."""

    async def ensure_collection(self, *, dimension: int) -> None:
        """Make the collection exist, holding vectors of exactly `dimension`.

        Idempotent. Called once at startup, never from a job.

        :raises VectorStoreError: if a collection exists with another width —
            a model change that has not been dealt with, never papered over.
        """
        ...

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Write these vectors, replacing any point with the same id.

        :raises VectorStoreError: if the write fails, or a record's width is
            not the collection's.
        """
        ...

    async def search(
        self,
        query_vector: Vector,
        *,
        owner_id: str,
        limit: int,
        score_threshold: float,
        document_ids: Sequence[str] | None = None,
    ) -> tuple[VectorMatch, ...]:
        """The nearest vectors **belonging to `owner_id`**, best first.

        `owner_id` is not optional and is applied by the server inside the
        query; `document_ids` can only narrow that further, never widen it.

        :raises VectorStoreError: if the search fails.
        """
        ...

    async def delete_document(self, *, document_id: str, owner_id: str) -> None:
        """Remove every vector of one document, scoped to its owner.

        Idempotent: deleting vectors that are not there is not an error.

        :raises VectorStoreError: if the delete fails.
        """
        ...

    async def count_for_document(self, *, document_id: str, owner_id: str) -> int:
        """How many vectors this owner's document has in the store.

        :raises VectorStoreError: if the count fails.
        """
        ...
