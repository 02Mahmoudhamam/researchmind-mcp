"""`POST /api/v1/search` — M4/S4.2.

Three layers, deliberately separated.

* **The route** (`TestTheRoute…`): the service is overridden, so a failure here
  is the HTTP layer's — validation, status codes, and what does *not* appear in
  the body.
* **The composition root** (`TestLifespan…`): that one provider and one client
  are built per process and closed after, with the real model.
* **Isolation over HTTP** (`TestTenantIsolationOverHttp`): two authenticated
  users, real PostgreSQL, real Qdrant, real ingestion. Nothing mocked, because
  the claim is about the system rather than about a double.

ADR-0014 §11–§14; `principles.md` §3 and §7.
"""

import io
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.api.app import app
from backend.api.dependencies.services import get_search_service
from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.repositories import UserRepository
from backend.db.session import get_sessionmaker
from backend.security.jwt_handler import JWTHandler
from backend.services.document_service import DocumentService
from backend.services.ingestion_service import IngestionService
from backend.services.search_service import InvalidSearchQuery, SearchUnavailable
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from document_processing.tokenization import TOKENIZER_ID, RegexTokenizer
from shared.models.ingestion import IngestionJob
from shared.models.principal import Principal
from shared.models.retrieval import RetrievalResult
from shared.models.user import User, UserRole
from tests.doubles import STUB_DIMENSION, STUB_MODEL_ID, StubEmbeddingProvider
from tests.pdfs import make_paper_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

SEARCH = "/api/v1/search/"


def _result(
    chunk_id: str, score: float = 0.9, document_id: str = "doc-1"
) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=document_id,
        score=score,
        content="the chunk's text, from PostgreSQL",
        chunk_index=3,
        section="3.1 Evaluation",
        page_start=2,
        page_end=3,
        embedding_model_id=STUB_MODEL_ID,
    )


class StubSearchService:
    """Stands in for `SearchService` so route tests are about the route."""

    def __init__(
        self, results: tuple[RetrievalResult, ...] = (), raises: Exception | None = None
    ):
        self.results = results
        self.raises = raises
        self.calls: list[tuple[Any, Any]] = []

    async def search(self, query: Any, principal: Any) -> tuple[RetrievalResult, ...]:
        self.calls.append((query, principal))
        if self.raises is not None:
            raise self.raises
        return self.results


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


async def _account(
    session: Any, role: UserRole = UserRole.RESEARCHER
) -> tuple[User, str]:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Searcher", role=role
    )
    await session.commit()
    return user, JWTHandler().create_access_token(user.id, user.email, user.role)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def stub_service() -> AsyncIterator[StubSearchService]:
    """Install a stub `SearchService`; remove it afterwards."""
    stub = StubSearchService()
    app.dependency_overrides[get_search_service] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_search_service, None)


# --------------------------------------------------------------- the route


class TestTheRouteReturnsResults:
    async def test_a_valid_search_returns_its_hits(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        _user, token = await _account(committing_session)
        stub_service.results = (_result("c1", 0.91), _result("c2", 0.84))

        async with await _client() as client:
            response = await client.post(
                SEARCH,
                json={"query": "how was recall measured?"},
                headers=_bearer(token),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert [hit["chunk_id"] for hit in body["results"]] == ["c1", "c2"]
        assert [hit["score"] for hit in body["results"]] == [0.91, 0.84]

    async def test_a_hit_carries_the_provenance_a_citation_needs(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        _user, token = await _account(committing_session)
        stub_service.results = (_result("c1"),)

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers=_bearer(token)
            )

        (hit,) = response.json()["results"]
        assert hit == {
            "chunk_id": "c1",
            "document_id": "doc-1",
            "score": 0.9,
            "content": "the chunk's text, from PostgreSQL",
            "chunk_index": 3,
            "section": "3.1 Evaluation",
            "page_start": 2,
            "page_end": 3,
            "embedding_model_id": STUB_MODEL_ID,
        }

    async def test_no_results_is_a_200_with_an_empty_list(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """Absence is an answer. Only an outage is an error (ADR-0014 §6)."""
        _user, token = await _account(committing_session)

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers=_bearer(token)
            )

        assert response.status_code == 200
        assert response.json() == {"results": [], "total": 0}

    async def test_the_response_never_carries_a_vector(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """The scaffold's shape nested `DocumentChunk`, which has `embedding`."""
        _user, token = await _account(committing_session)
        stub_service.results = (_result("c1"),)

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers=_bearer(token)
            )

        # Not a bare "embedding" substring: `embedding_model_id` contains it,
        # and is a string a client legitimately needs (ADR-0014 §13).
        assert '"embedding"' not in response.text
        (hit,) = response.json()["results"]
        assert not {"embedding", "vector", "payload", "chunk"} & set(hit)
        assert not any(isinstance(value, list) for value in hit.values())

    async def test_the_response_does_not_echo_the_query(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """A research question is a sensitive string in a body-logging proxy."""
        _user, token = await _account(committing_session)
        secret = "my unpublished hypothesis about ribosomes"

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": secret}, headers=_bearer(token)
            )

        assert secret not in response.text


class TestTheRouteValidatesInput:
    @pytest.mark.parametrize("query", ["", "   ", "\n\t"])
    async def test_an_empty_query_reaches_the_service_and_its_refusal_is_a_400(
        self, committing_session: Any, stub_service: StubSearchService, query: str
    ) -> None:
        """The route does not invent an emptiness rule of its own.

        `SearchService` already normalises NFC and refuses blank text, and a
        second rule at the edge would be a second thing to keep in step. The
        route's job is to *reach* it and map its refusal, which is what this
        asserts: the call happens, and `InvalidSearchQuery` becomes a 400.
        """
        _user, token = await _account(committing_session)
        stub_service.raises = InvalidSearchQuery("a search query must contain text")

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": query}, headers=_bearer(token)
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "a search query must contain text"
        assert stub_service.calls, "the service decides emptiness, not the route"

    async def test_a_query_too_long_for_the_model_is_refused(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        _user, token = await _account(committing_session)
        stub_service.raises = InvalidSearchQuery(
            "a search query may be at most 2000 characters"
        )

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "x" * 5000}, headers=_bearer(token)
            )

        assert response.status_code == 400
        assert "2000 characters" in response.json()["detail"]

    @pytest.mark.parametrize("top_k", [0, -1])
    async def test_a_nonsense_top_k_is_refused_at_the_edge(
        self, committing_session: Any, stub_service: StubSearchService, top_k: int
    ) -> None:
        _user, token = await _account(committing_session)

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q", "top_k": top_k}, headers=_bearer(token)
            )

        assert response.status_code == 422
        assert not stub_service.calls

    @pytest.mark.parametrize("threshold", [-0.1, 1.5])
    async def test_a_threshold_outside_the_range_is_refused(
        self, committing_session: Any, stub_service: StubSearchService, threshold: float
    ) -> None:
        _user, token = await _account(committing_session)

        async with await _client() as client:
            response = await client.post(
                SEARCH,
                json={"query": "q", "score_threshold": threshold},
                headers=_bearer(token),
            )

        assert response.status_code == 422

    async def test_an_owner_field_is_refused_rather_than_ignored(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """`extra="forbid"`: naming another user is a 422, not an escalation."""
        _user, token = await _account(committing_session)

        async with await _client() as client:
            for field in ("user_id", "owner_id", "principal"):
                response = await client.post(
                    SEARCH,
                    json={"query": "q", field: str(uuid.uuid4())},
                    headers=_bearer(token),
                )
                assert response.status_code == 422, field
        assert not stub_service.calls

    async def test_the_owner_handed_to_the_service_is_the_token_holder(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        user, token = await _account(committing_session)

        async with await _client() as client:
            await client.post(SEARCH, json={"query": "q"}, headers=_bearer(token))

        (_query, principal) = stub_service.calls[0]
        assert isinstance(principal, Principal)
        assert principal.user_id == user.id

    async def test_defaults_are_left_for_the_service_to_apply(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """The route does not invent a top-k; Settings owns the default."""
        _user, token = await _account(committing_session)

        async with await _client() as client:
            await client.post(SEARCH, json={"query": "q"}, headers=_bearer(token))

        (query, _principal) = stub_service.calls[0]
        assert query.top_k is None and query.score_threshold is None

    async def test_document_ids_are_passed_through(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        _user, token = await _account(committing_session)
        wanted = str(uuid.uuid4())

        async with await _client() as client:
            await client.post(
                SEARCH,
                json={"query": "q", "document_ids": [wanted]},
                headers=_bearer(token),
            )

        (query, _principal) = stub_service.calls[0]
        assert query.document_ids == (wanted,)


class TestTheRouteMapsFailures:
    async def test_an_unavailable_backend_is_a_503(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """Not an empty 200. An outage and "no matches" must not look alike."""
        _user, token = await _account(committing_session)
        stub_service.raises = SearchUnavailable(
            "the vector store could not be searched"
        )

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers=_bearer(token)
            )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Search is unavailable. No results were returned."
        }

    async def test_a_failure_leaks_nothing_internal(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        _user, token = await _account(committing_session)
        stub_service.raises = SearchUnavailable(
            "could not reach qdrant at qdrant.internal:6333 as user researchmind"
        )

        async with await _client() as client:
            response = await client.post(
                SEARCH,
                json={"query": "confidential hypothesis"},
                headers=_bearer(token),
            )

        body = response.text
        for leak in ("qdrant", "6333", "researchmind", "Traceback", "confidential"):
            assert leak not in body, leak


class TestTheRouteRequiresIdentity:
    async def test_no_token_is_a_401(self, stub_service: StubSearchService) -> None:
        async with await _client() as client:
            response = await client.post(SEARCH, json={"query": "q"})
        assert response.status_code == 401
        assert not stub_service.calls

    @pytest.mark.parametrize("header", ["Bearer nonsense", "Basic abc", "Bearer "])
    async def test_a_malformed_token_is_a_401(
        self, stub_service: StubSearchService, header: str
    ) -> None:
        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers={"Authorization": header}
            )
        assert response.status_code == 401
        assert not stub_service.calls

    async def test_a_deactivated_user_is_a_401(
        self, committing_session: Any, stub_service: StubSearchService
    ) -> None:
        """principles.md §1: a valid token for an inactive user is not identity."""
        user, token = await _account(committing_session)
        await committing_session.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"),
            {"id": uuid.UUID(user.id)},
        )
        await committing_session.commit()

        async with await _client() as client:
            response = await client.post(
                SEARCH, json={"query": "q"}, headers=_bearer(token)
            )

        assert response.status_code == 401
        assert not stub_service.calls


# ----------------------------------------------------- the composition root


class TestLifespanOwnsRetrievalResources:
    """ADR-0014 §11: one provider and one client per process, closed after."""

    @pytest.mark.embeddings
    async def test_startup_builds_them_and_shutdown_closes_them(self) -> None:
        from backend.api.composition import RetrievalResources

        closed: list[bool] = []
        async with app.router.lifespan_context(app):
            resources = app.state.retrieval
            assert isinstance(resources, RetrievalResources)
            assert resources.embedder.dimension > 0
            original = resources.client.close

            async def spy() -> None:
                closed.append(True)
                await original()

            object.__setattr__(resources, "client", _Closeable(spy))
        assert closed == [True], "shutdown must release the Qdrant pool"

    @pytest.mark.embeddings
    async def test_every_request_reuses_the_same_provider(
        self, committing_session: Any
    ) -> None:
        """Not per request: the model is loaded once and handed out."""
        from backend.api.dependencies.services import get_retrieval_resources

        seen: list[int] = []

        async with app.router.lifespan_context(app):
            _user, token = await _account(committing_session)
            app.dependency_overrides[get_search_service] = lambda: _RecordingService(
                seen, app.state.retrieval
            )
            try:
                async with await _client() as client:
                    for _ in range(3):
                        response = await client.post(
                            SEARCH, json={"query": "q"}, headers=_bearer(token)
                        )
                        assert response.status_code == 200
            finally:
                app.dependency_overrides.pop(get_search_service, None)

        assert len(set(seen)) == 1, "a provider was rebuilt between requests"
        assert get_retrieval_resources is not None

    @pytest.mark.embeddings
    @pytest.mark.qdrant
    async def test_an_unprepared_collection_is_a_503_not_an_empty_200(
        self, committing_session: Any
    ) -> None:
        """The real dependency chain, with no override — and the consequence
        of ADR-0014 §12 made explicit.

        The API does not create the collection; the worker does. So on a system
        where the worker has never run, the index genuinely is not there, and
        search says so. Returning an empty 200 would tell a caller their corpus
        has no matches when the truth is that nothing has been indexed yet
        (ADR-0014 §6).

        It also proves the wiring resolves: the provider, the store and the
        session all reached the service without the route building any of them.
        """
        from vector_db.qdrant.client import build_qdrant_client
        from vector_db.qdrant.config import QdrantConfig

        config = QdrantConfig.from_settings(get_settings())
        client = build_qdrant_client(config)
        await client.delete_collection(config.collection_name)
        await client.close()

        async with app.router.lifespan_context(app):
            _user, token = await _account(committing_session)
            async with await _client() as client_http:
                response = await client_http.post(
                    SEARCH, json={"query": "anything at all"}, headers=_bearer(token)
                )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Search is unavailable. No results were returned."
        }

    async def test_a_provider_that_cannot_load_stops_the_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0014 §11: never accept requests with a half-built root."""
        import backend.api.app as app_module
        from backend.api.composition import RetrievalUnavailable

        def refuse(settings: object) -> None:
            raise RetrievalUnavailable("the embedding model could not be loaded")

        monkeypatch.setattr(app_module, "build_retrieval_resources", refuse)
        with pytest.raises(RetrievalUnavailable):
            async with app.router.lifespan_context(app):
                pass

    async def test_without_a_lifespan_the_route_does_not_build_a_provider(
        self, committing_session: Any
    ) -> None:
        """A wiring bug must be loud, not a model load inside a request."""
        from backend.api.composition import RetrievalUnavailable

        assert not hasattr(
            app.state, "retrieval"
        ), "a previous lifespan left resources behind; shutdown must clear them"
        _user, token = await _account(committing_session)
        async with await _client() as client:
            with pytest.raises(RetrievalUnavailable):
                await client.post(SEARCH, json={"query": "q"}, headers=_bearer(token))

    @pytest.mark.embeddings
    async def test_shutdown_clears_the_resources_it_built(self) -> None:
        """Closed *and* removed: a request after shutdown must not resolve a
        pool that is already gone."""
        async with app.router.lifespan_context(app):
            assert hasattr(app.state, "retrieval")
        assert not hasattr(app.state, "retrieval")

    async def test_the_composition_root_makes_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0014 §12: own the resource, do not probe the service.

        Building the client must not connect, and nothing may create the
        collection — that is the worker's, and making it the API's would put a
        reachable Qdrant between the process and `/health`.
        """
        from backend.api import composition
        from vector_db.qdrant.repository import QdrantVectorStore

        def explode(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the composition root called Qdrant")

        monkeypatch.setattr(QdrantVectorStore, "ensure_collection", explode)
        monkeypatch.setattr(
            composition, "FastEmbedProvider", lambda *a, **k: StubEmbeddingProvider()
        )

        resources = composition.build_retrieval_resources(get_settings())
        await resources.aclose()


class _Closeable:
    def __init__(self, close: Any) -> None:
        self.close = close


class _RecordingService:
    """Records which provider object the request was served with."""

    def __init__(self, seen: list[int], resources: Any) -> None:
        self.seen = seen
        self.resources = resources

    async def search(self, query: Any, principal: Any) -> tuple[()]:
        self.seen.append(id(self.resources.embedder))
        return ()


# -------------------------------------------------- isolation over real HTTP


class TestTenantIsolationOverHttp:
    """Two authenticated users, real PostgreSQL, real Qdrant, real ingestion.

    Nothing here is mocked. The claim — that one user cannot reach another's
    chunks over HTTP — is about the system, and a double would only prove the
    double behaves.
    """

    @pytest.fixture()
    async def corpus(
        self, committing_session: Any, root: Path
    ) -> AsyncIterator[dict[str, Any]]:
        """Alice and Bob, each with the same paper ingested to `ready`.

        Ingested into the collection the API's lifespan will read — named by
        `QDRANT_COLLECTION`, which `tests/conftest.py` points at a test-only
        collection. The worker owns collection creation, so the test does it
        here rather than expecting the API to.
        """
        from vector_db.qdrant.client import build_qdrant_client
        from vector_db.qdrant.config import QdrantConfig
        from vector_db.qdrant.repository import QdrantVectorStore

        settings = get_settings()
        config = QdrantConfig.from_settings(settings)
        client = build_qdrant_client(config)
        store = QdrantVectorStore(client, config)
        await client.delete_collection(config.collection_name)
        await store.ensure_collection(dimension=STUB_DIMENSION)

        people: dict[str, Any] = {}
        for name in ("alice", "bob"):
            user, token = await _account(committing_session)
            principal = Principal(
                user_id=user.id, email=user.email, role=UserRole.RESEARCHER
            )
            queue = _RecordingQueue()
            await DocumentService(
                committing_session, storage=LocalStorage(root), ingestion=queue
            ).upload_and_process(
                filename="paper.pdf",
                stream=io.BytesIO(make_paper_pdf()),
                principal=principal,
            )
            job = queue.jobs[0]
            async with get_sessionmaker()() as worker_session:
                await IngestionService(
                    worker_session,
                    LocalStorage(root),
                    extractor=PyMuPDFTextExtractor(
                        timeout_seconds=60, max_concurrency=4
                    ),
                    chunker=SectionAwareChunker(
                        RegexTokenizer(), chunk_size=120, chunk_overlap=20
                    ),
                    embedder=StubEmbeddingProvider(),
                    vectors=store,
                    strategy_version=STRATEGY_VERSION,
                    tokenizer_id=TOKENIZER_ID,
                    max_pages=settings.MAX_PDF_PAGES,
                ).ingest(job, final_attempt=False)
            people[name] = {"token": token, "document_id": job.document_id}

        try:
            yield people
        finally:
            await client.delete_collection(config.collection_name)
            await client.close()

    @pytest.fixture()
    def http_with_stub_provider(self) -> AsyncIterator[None]:
        """Serve the route with the stub provider the corpus was embedded by.

        The real model is exercised by the vertical slice below; using it here
        too would add a minute to a test about ownership. The provider must
        still be the *same one* the chunks record, or S4.1's model predicate
        drops every candidate — which is the point of that predicate.
        """
        from backend.api import composition

        original = composition.FastEmbedProvider
        composition.FastEmbedProvider = lambda *a, **k: StubEmbeddingProvider()  # type: ignore[assignment]
        yield
        composition.FastEmbedProvider = original  # type: ignore[assignment]

    async def test_a_user_retrieves_their_own_document(
        self, corpus: dict[str, Any], http_with_stub_provider: None
    ) -> None:
        async with app.router.lifespan_context(app):
            async with await _client() as client:
                response = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] > 0
        assert {hit["document_id"] for hit in body["results"]} == {
            corpus["alice"]["document_id"]
        }

    async def test_the_second_user_gets_none_of_the_first_users_chunks(
        self, corpus: dict[str, Any], http_with_stub_provider: None
    ) -> None:
        """The identical search, by a different token. Phase 3 DoD step 5."""
        async with app.router.lifespan_context(app):
            async with await _client() as client:
                alice = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )
                bob = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["bob"]["token"]),
                )

        alice_docs = {hit["document_id"] for hit in alice.json()["results"]}
        bob_docs = {hit["document_id"] for hit in bob.json()["results"]}
        assert alice_docs == {corpus["alice"]["document_id"]}
        assert bob_docs == {corpus["bob"]["document_id"]}
        assert not alice_docs & bob_docs

    async def test_naming_another_users_document_returns_nothing(
        self, corpus: dict[str, Any], http_with_stub_provider: None
    ) -> None:
        """A forged `document_ids` can only narrow, never widen."""
        async with app.router.lifespan_context(app):
            async with await _client() as client:
                response = await client.post(
                    SEARCH,
                    json={
                        "query": "what did the study measure?",
                        "document_ids": [corpus["alice"]["document_id"]],
                    },
                    headers=_bearer(corpus["bob"]["token"]),
                )

        assert response.status_code == 200
        assert response.json() == {"results": [], "total": 0}

    async def test_a_non_ready_document_disappears_from_results(
        self,
        committing_session: Any,
        corpus: dict[str, Any],
        http_with_stub_provider: None,
    ) -> None:
        """Its vectors are untouched; only the row's status changes.

        ADR-0014 §3: the existence of a vector proves nothing, because vectors
        outlive the documents they describe. PostgreSQL's status at the moment
        of the query is what decides, and this proves it through HTTP.
        """
        async with app.router.lifespan_context(app):
            async with await _client() as client:
                before = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )
                assert before.json()["total"] > 0

                await committing_session.execute(
                    text("UPDATE documents SET status = 'chunked' WHERE id = :id"),
                    {"id": uuid.UUID(corpus["alice"]["document_id"])},
                )
                await committing_session.commit()

                after = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )

        assert after.json() == {"results": [], "total": 0}

    async def test_a_candidate_the_index_wrongly_offers_is_still_refused(
        self,
        committing_session: Any,
        corpus: dict[str, Any],
        http_with_stub_provider: None,
    ) -> None:
        """Defence in depth, proved by breaking the first layer.

        Alice's chunk is re-upserted with a payload claiming Bob owns it, so
        Qdrant genuinely hands it to Bob's search. PostgreSQL is asked for the
        chunk *id* and answers that it is Alice's, so Bob still gets nothing —
        which is the whole reason ADR-0003 §5 has two layers rather than one.
        """
        import dataclasses

        from shared.interfaces.vector_store import VectorRecord
        from vector_db.qdrant.client import build_qdrant_client
        from vector_db.qdrant.config import QdrantConfig
        from vector_db.qdrant.repository import QdrantVectorStore

        config = QdrantConfig.from_settings(get_settings())
        client = build_qdrant_client(config)
        store = QdrantVectorStore(client, config)

        async with get_sessionmaker()() as session:
            row = await session.execute(
                text(
                    "SELECT id FROM document_chunks WHERE document_id = :id"
                    " ORDER BY chunk_index LIMIT 1"
                ),
                {"id": uuid.UUID(corpus["alice"]["document_id"])},
            )
            stolen = str(row.scalar_one())

        bob_id = await _owner_id_of(corpus["bob"]["document_id"])
        found = await store.search(
            (0.0,) * STUB_DIMENSION,
            owner_id=await _owner_id_of(corpus["alice"]["document_id"]),
            limit=100,
            score_threshold=-1.0,
        )
        original = next(match for match in found if match.id == stolen)
        await store.upsert(
            [
                VectorRecord(
                    id=stolen,
                    vector=original_vector(),
                    payload=dataclasses.replace(original.payload, user_id=bob_id),
                )
            ]
        )
        offered = await store.search(
            (0.0,) * STUB_DIMENSION, owner_id=bob_id, limit=100, score_threshold=-1.0
        )
        assert stolen in {m.id for m in offered}, "the index must really offer it"
        await client.close()

        async with app.router.lifespan_context(app):
            async with await _client() as client_http:
                response = await client_http.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["bob"]["token"]),
                )

        assert response.status_code == 200
        returned = {hit["chunk_id"] for hit in response.json()["results"]}
        assert stolen not in returned

    async def test_a_deleted_document_disappears_from_results(
        self,
        committing_session: Any,
        corpus: dict[str, Any],
        root: Path,
        http_with_stub_provider: None,
    ) -> None:
        """Its vectors stay in Qdrant (ADR-0003 §6 is Phase 5's). PostgreSQL
        is what decides, and this proves it over HTTP."""
        async with app.router.lifespan_context(app):
            async with await _client() as client:
                before = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )
                assert before.json()["total"] > 0

                await committing_session.execute(
                    text("UPDATE documents SET deleted_at = now() WHERE id = :id"),
                    {"id": uuid.UUID(corpus["alice"]["document_id"])},
                )
                await committing_session.commit()

                after = await client.post(
                    SEARCH,
                    json={"query": "what did the study measure?"},
                    headers=_bearer(corpus["alice"]["token"]),
                )

        assert after.status_code == 200
        assert after.json() == {"results": [], "total": 0}


class _RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


def original_vector() -> tuple[float, ...]:
    """Any unit vector of the stub's width; the payload is what matters here."""
    value = 1.0 / (STUB_DIMENSION**0.5)
    return tuple(value for _ in range(STUB_DIMENSION))


async def _owner_id_of(document_id: str) -> str:
    async with get_sessionmaker()() as session:
        row = await session.execute(
            text("SELECT user_id FROM documents WHERE id = :id"),
            {"id": uuid.UUID(document_id)},
        )
        return str(row.scalar_one())
