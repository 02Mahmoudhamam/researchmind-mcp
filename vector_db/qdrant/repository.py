"""The Qdrant adapter — the only module in the tree that speaks Qdrant.

`VectorStore` is what the application depends on (ADR-0005 §3): nothing above
this file imports `qdrant_client`, builds a `Filter`, or knows that a payload
is a dictionary on the wire. An architecture test asserts it.

Two rules run through everything here.

**The owner is in the query.** Every operation that can reach a stored vector
takes `owner_id` and puts it inside the `Filter` the server evaluates
(ADR-0003 §5, ADR-0013 §5). Nothing is fetched and then filtered in Python:
post-filtering means the wrong rows were already read, and one forgotten line
returns them.

**A vendor exception never escapes.** Qdrant's errors carry the host, the URL
and sometimes the request body. They are turned into `VectorStoreError` with a
message built here, because that message is written to `failure_reason` and the
worker's logs.
"""

from typing import Sequence

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from shared.interfaces.embedding import Vector
from shared.interfaces.vector_store import (
    VectorMatch,
    VectorPayload,
    VectorRecord,
    VectorStoreError,
)
from vector_db.qdrant.config import QdrantConfig

# The payload fields the owner filter and document scoping match on. Qdrant
# will filter without an index, but by scanning; these make it a lookup, and
# the isolation invariant is on this path for every single query.
_INDEXED_FIELDS = ("user_id", "document_id")

# Cosine, because the model's vectors are L2-normalised (ADR-0013): cosine and
# dot agree, and cosine stays correct if a later model's are not.
_DISTANCE = models.Distance.COSINE


class QdrantVectorStore:
    """`VectorStore` backed by a Qdrant collection."""

    def __init__(self, client: AsyncQdrantClient, config: QdrantConfig) -> None:
        self._client = client
        self._collection = config.collection_name
        self._config = config

    # --- collection -----------------------------------------------------------

    async def ensure_collection(self, *, dimension: int) -> None:
        """Create the collection at `dimension`, or check the one that exists.

        Called once, from the worker's startup. A collection built for another
        model is **not** reused: its vectors are in a different space and of a
        different width, so every search against it would be wrong or refused.
        """
        if dimension <= 0:
            raise VectorStoreError("a collection needs a positive dimension")
        try:
            if await self._client.collection_exists(self._collection):
                existing = await self._dimension_of_existing_collection()
                if existing != dimension:
                    raise VectorStoreError(
                        f"collection {self._collection!r} holds vectors of "
                        f"{existing} numbers, but the embedding model produces "
                        f"{dimension}"
                    )
                return
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(size=dimension, distance=_DISTANCE),
            )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                self._failed("prepare the collection", exc)
            ) from None

        for field in _INDEXED_FIELDS:
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:
                raise VectorStoreError(
                    self._failed(f"index the {field} payload field", exc)
                ) from None

    async def _dimension_of_existing_collection(self) -> int:
        info = await self._client.get_collection(self._collection)
        params = info.config.params.vectors
        if not isinstance(params, models.VectorParams):
            raise VectorStoreError(
                f"collection {self._collection!r} does not hold a single "
                "unnamed vector, which is what this store writes"
            )
        return int(params.size)

    # --- writing --------------------------------------------------------------

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Write these vectors. The point id is the chunk id, so this replaces.

        `wait=True`: the call returns when the write is durable. ADR-0013 §3
        sets `ready` on the strength of this having happened, so returning
        before Qdrant has the points would make the status a guess.
        """
        if not records:
            return
        points = [
            models.PointStruct(
                id=record.id,
                vector=list(record.vector),
                payload=record.payload.as_mapping(),
            )
            for record in records
        ]
        try:
            await self._client.upsert(
                collection_name=self._collection, points=points, wait=True
            )
        except Exception as exc:
            raise VectorStoreError(
                self._failed(f"write {len(points)} vector(s)", exc)
            ) from None

    # --- reading --------------------------------------------------------------

    async def search(
        self,
        query_vector: Vector,
        *,
        owner_id: str,
        limit: int,
        score_threshold: float,
        document_ids: Sequence[str] | None = None,
    ) -> tuple[VectorMatch, ...]:
        """The owner's nearest vectors, best first."""
        if limit <= 0:
            raise VectorStoreError("a search needs a positive limit")
        try:
            response = await self._client.query_points(
                collection_name=self._collection,
                query=list(query_vector),
                query_filter=self._owned_by(owner_id, document_ids),
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise VectorStoreError(self._failed("search", exc)) from None

        matches = []
        for point in response.points:
            payload = VectorPayload.from_mapping(point.payload or {})
            # Defence in depth, not the filter. The filter above is what keeps
            # tenants apart; this refuses — loudly — if a point ever comes back
            # that it should have excluded, rather than dropping it quietly and
            # leaving a broken query looking like an empty result.
            if payload.user_id != owner_id:
                raise VectorStoreError(
                    "the vector store returned a point belonging to another owner"
                )
            matches.append(
                VectorMatch(id=str(point.id), score=float(point.score), payload=payload)
            )
        return tuple(matches)

    async def count_for_document(self, *, document_id: str, owner_id: str) -> int:
        """How many vectors this owner's document has."""
        try:
            result = await self._client.count(
                collection_name=self._collection,
                count_filter=self._owned_by(owner_id, [document_id]),
                exact=True,
            )
        except Exception as exc:
            raise VectorStoreError(self._failed("count vectors", exc)) from None
        return int(result.count)

    # --- deleting -------------------------------------------------------------

    async def delete_document(self, *, document_id: str, owner_id: str) -> None:
        """Remove one document's vectors — this owner's, and only theirs.

        Owner-scoped although a document id is already unique, because that
        uniqueness is PostgreSQL's invariant and this is a different store. A
        forged id must delete nothing, not someone else's document.
        """
        try:
            await self._client.delete(
                collection_name=self._collection,
                points_selector=models.FilterSelector(
                    filter=self._owned_by(owner_id, [document_id])
                ),
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(self._failed("delete vectors", exc)) from None

    # --- the filter -----------------------------------------------------------

    @staticmethod
    def _owned_by(
        owner_id: str, document_ids: Sequence[str] | None = None
    ) -> models.Filter:
        """The isolation invariant, as the server evaluates it.

        `must`, never `should`: the owner condition cannot be satisfied by some
        other condition matching instead. `document_ids` is appended to the
        same `must`, so it can only narrow what the owner condition already
        allows.
        """
        if not owner_id:
            raise VectorStoreError("a query without an owner is not a query")
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="user_id", match=models.MatchValue(value=owner_id)
            )
        ]
        if document_ids is not None:
            conditions.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=[str(one) for one in document_ids]),
                )
            )
        return models.Filter(must=conditions)

    # --- errors ---------------------------------------------------------------

    def _failed(self, what: str, exc: Exception) -> str:
        """A message safe to log and to store in `failure_reason`.

        The exception's own text is deliberately dropped: `UnexpectedResponse`
        renders the URL, the headers and the response body, and the body can
        contain the payload that was being written.
        """
        kind = (
            "refused by the vector store"
            if isinstance(exc, UnexpectedResponse)
            else type(exc).__name__
        )
        return f"could not {what} in collection {self._collection!r} ({kind})"
