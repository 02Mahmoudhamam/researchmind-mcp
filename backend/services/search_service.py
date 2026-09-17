"""Semantic search — M4/S4.1, the read half of RAG.

ADR-0003 §5 and `docs/security/principles.md` §3 fix the shape in identical
words: Qdrant filtered on `user_id` is the *fast* path, and re-validating every
returned chunk id against PostgreSQL is the *correct* one. ADR-0014 settles what
that means in practice, and this module is it.

    Principal ──► validate ──► embed ──► VectorStore ──► PostgreSQL ──► results
                                          (ids + scores)   (everything else)

**Qdrant proposes; PostgreSQL disposes.** The vector store returns chunk ids and
scores. Its payload is *not read* here — not its `user_id`, not its
`document_id`, not for comparison and not for a check. Every fact behind a
result, including which document a chunk belongs to and what it says, is read
from the database in one owner-scoped statement. A stale payload, a payload from
an older pipeline and a forged payload are therefore the same thing: a chunk id,
which either resolves to a row this principal owns or does not.

The owner comes from the authenticated `Principal` and from nowhere else.
`RetrievalQuery` has no owner field, so "search as someone else" is not a
request that can be expressed.

This module imports no `qdrant_client`, no `fastembed`, no driver — only the
`EmbeddingProvider` and `VectorStore` protocols, as `IngestionService` imports
only `Storage` and `PdfTextExtractor`. An architecture test asserts it.
"""

import unicodedata

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import DocumentChunkRepository
from shared.interfaces.embedding import (
    SPECIAL_TOKENS_PER_SEQUENCE,
    EmbeddingError,
    EmbeddingProvider,
)
from shared.interfaces.vector_store import VectorStore, VectorStoreError
from shared.models.principal import Principal
from shared.models.retrieval import RetrievalQuery, RetrievalResult
from shared.utils.logger import get_logger

_log = get_logger(__name__)


class InvalidSearchQuery(ValueError):
    """The query itself is unusable — empty, or longer than the model reads.

    The caller's fault and not retryable, so it is a different type from
    `SearchUnavailable`. **The message never contains the query text**, which
    is a user's research question and is not logged either.
    """


class SearchUnavailable(Exception):
    """A dependency of search failed.

    Deliberately *not* an empty result (ADR-0014 §6). Stale vectors degrade to
    fewer results; an unreachable index is an outage, and a caller that cannot
    tell the two apart will present the second as the first — which in M5 means
    an answer that cites nothing and sounds certain.

    Carries no host, URL, credential, driver message or stack trace: it is
    built here, from nothing the failure supplied.
    """


class SearchService:
    """Owner-scoped semantic search over a user's own corpus."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: EmbeddingProvider,
        vectors: VectorStore,
        top_k: int,
        score_threshold: float,
        max_query_chars: int,
    ) -> None:
        """
        :param embedder: embeds the query. **The same provider that embedded
            the documents** (ADR-0005 §5) — a second model would compare two
            vector spaces. Validation enforces this from the other side too, by
            refusing chunks recorded against another model.
        :param vectors: the index. Its `search` requires an `owner_id`.
        :param top_k: how many candidates to ask the index for, when the query
            does not say. Candidates, not results: validation may drop some.
        :param score_threshold: the similarity below which a candidate is not
            worth returning.
        :param max_query_chars: a cheap bound applied before tokenizing.
        """
        self._session = session
        self._embedder = embedder
        self._vectors = vectors
        self._top_k = top_k
        self._score_threshold = score_threshold
        self._max_query_chars = max_query_chars
        self._chunks = DocumentChunkRepository(session)

    async def search(
        self, query: RetrievalQuery, principal: Principal
    ) -> tuple[RetrievalResult, ...]:
        """The user's own chunks that best match their query, best first.

        :raises InvalidSearchQuery: the query is empty or too long.
        :raises SearchUnavailable: the embedding provider, the vector store or
            the database failed. Never returned as an empty result.
        """
        text = self._acceptable(query.text)
        top_k = query.top_k or self._top_k
        threshold = (
            self._score_threshold
            if query.score_threshold is None
            else query.score_threshold
        )

        # Embedding and the vector search are network and CPU work. No
        # transaction is open across either (ADR-0014 §10).
        try:
            vector = await self._embedder.embed_query(text)
        except EmbeddingError:
            # The provider's own message is dropped: it is built from the text
            # it was given, and that text is the user's question.
            raise SearchUnavailable("the query could not be embedded") from None

        try:
            candidates = await self._vectors.search(
                vector,
                owner_id=principal.user_id,
                limit=top_k,
                score_threshold=threshold,
                document_ids=(
                    None if query.document_ids is None else list(query.document_ids)
                ),
            )
        except VectorStoreError:
            raise SearchUnavailable("the vector store could not be searched") from None

        # Deduplicate before validating, so a repeated id cannot consume two
        # slots of the batch. First occurrence wins, which is the highest score
        # because the store returns them in descending order (ADR-0014 §5).
        ranked: dict[str, float] = {}
        for candidate in candidates:
            ranked.setdefault(candidate.id, candidate.score)
        if not ranked:
            return ()

        try:
            validated = await self._chunks.validate_for_retrieval(
                list(ranked),
                principal.user_id,
                embedding_model_id=self._embedder.model_id,
                dimension=self._embedder.dimension,
            )
        except Exception:
            raise SearchUnavailable(
                "the search results could not be validated"
            ) from None
        finally:
            # A read-only transaction; nothing here writes, and it is closed
            # before returning rather than held for the caller's convenience.
            await self._session.rollback()

        # The index's order, with the dropped candidates removed and the
        # survivors' relative order untouched. The database returned its rows
        # in whatever order suited it, and that order is not used.
        results = tuple(
            RetrievalResult.of(validated[chunk_id], score)
            for chunk_id, score in ranked.items()
            if chunk_id in validated
        )

        dropped = len(ranked) - len(results)
        if dropped:
            # Expected, not alarming: vectors outlive the documents they
            # describe (ADR-0013 defers deletion), so a candidate whose
            # document was deleted or is no longer `ready` is dropped here.
            # Logged as a count, never as ids or text.
            _log.info(
                "search.candidates_dropped",
                owner_id=principal.user_id,
                candidates=len(ranked),
                returned=len(results),
            )
        return results

    def _acceptable(self, raw: str) -> str:
        """The query, normalised — or a refusal. Never logs what it was given.

        NFC because that is the form the corpus is in: `pdf_parser.py`
        normalises extracted text NFC, so query and documents are compared in
        one normal form rather than two.
        """
        text = unicodedata.normalize("NFC", raw).strip()
        if not text:
            raise InvalidSearchQuery("a search query must contain text")
        if len(text) > self._max_query_chars:
            raise InvalidSearchQuery(
                f"a search query may be at most {self._max_query_chars} characters"
            )
        # The real limit, and the reason the character cap above is only a
        # cheap first pass: past `max_input_tokens` the model truncates
        # silently, and the vector would describe a question nobody asked. The
        # same guard S3.5 applies to chunks (ADR-0013 §1, ADR-0014 §8).
        budget = self._embedder.max_input_tokens - SPECIAL_TOKENS_PER_SEQUENCE
        if self._embedder.tokenizer.count(text) > budget:
            raise InvalidSearchQuery(
                f"a search query may be at most {budget} tokens for "
                f"{self._embedder.model_id}"
            )
        return text
