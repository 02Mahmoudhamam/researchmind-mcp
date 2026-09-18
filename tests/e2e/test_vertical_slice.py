"""The Phase 3 vertical slice, steps 1–5 — M4/S4.2.

`COMPLETION_PLAN.md`'s Definition of Done for Phase 3 names this file. It has
been an empty package since the repository was created; this is the first part
of it that can exist, because step 4 needs an HTTP search endpoint and step 6
needs an agent (M5) and step 7 needs MCP (M6).

    1. Register a user, obtain a token.
    2. Upload a real PDF → 202 and a document id.
    3. Poll until `status == "ready"`.
    4. Search → ≥1 result with a score, a section, and a document the caller owns.
    5. A second user issuing the identical search → zero results.

Nothing is substituted. Real PostgreSQL, real Qdrant, the real FastEmbed model,
the real ingestion pipeline, and HTTP through the real application including its
lifespan. Registration and login go through `/api/v1/auth`, so the token is one
the system actually issues rather than one the test mints.

Steps 6 and 7 are M5's and M6's. They are not stubbed here: an empty assertion
would claim coverage this milestone has not earned.
"""

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.api.app import app
from backend.config.settings import get_settings
from backend.db.engine import get_engine
from backend.db.session import get_sessionmaker
from backend.ingestion.queue import ingestion_job_id
from backend.services.ingestion_service import IngestionService
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.pdf_parser import PyMuPDFTextExtractor
from shared.models.ingestion import IngestionJob
from tests.pdfs import make_paper_pdf
from vector_db.qdrant.client import build_qdrant_client
from vector_db.qdrant.config import QdrantConfig
from vector_db.qdrant.repository import QdrantVectorStore

pytestmark = [
    pytest.mark.db,
    pytest.mark.qdrant,
    pytest.mark.embeddings,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

PASSWORD = "correct-horse-battery-staple"
QUESTION = "What did the study measure?"


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


@pytest.fixture
async def collection(embedding_provider: Any) -> AsyncIterator[QdrantVectorStore]:
    """The collection the API will read, prepared as the worker would.

    The API deliberately does not create it (ADR-0014 §12), so something has to
    stand in for the worker's startup. That is the only thing standing in for
    anything in this file.
    """
    config = QdrantConfig.from_settings(get_settings())
    client = build_qdrant_client(config)
    store = QdrantVectorStore(client, config)
    await client.delete_collection(config.collection_name)
    await store.ensure_collection(dimension=embedding_provider.dimension)
    try:
        yield store
    finally:
        await client.delete_collection(config.collection_name)
        await client.close()


class _CapturingQueue:
    """Captures the job the API enqueues, so the worker can run it here."""

    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def enqueue(self, job: IngestionJob) -> bool:
        self.jobs.append(job)
        return True


async def _register_and_login(client: AsyncClient, email: str) -> str:
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Researcher"},
    )
    assert registered.status_code in (200, 201), registered.text

    logged_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text
    return str(logged_in.json()["access_token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _run_the_worker(
    job: IngestionJob, root: Path, store: Any, provider: Any
) -> None:
    """One delivery, through the real ingestion service.

    The same collaborators `backend/ingestion/worker.py` builds in its startup,
    constructed here because the test owns the loop rather than ARQ. The
    pipeline itself — verify, parse, chunk, embed, upsert, commit `ready` — is
    untouched.
    """
    async with get_sessionmaker()() as session:
        result = await IngestionService(
            session,
            LocalStorage(root),
            extractor=PyMuPDFTextExtractor(timeout_seconds=60, max_concurrency=4),
            chunker=SectionAwareChunker(
                provider.tokenizer,
                chunk_size=get_settings().CHUNK_SIZE_TOKENS,
                chunk_overlap=get_settings().CHUNK_OVERLAP_TOKENS,
            ),
            embedder=provider,
            vectors=store,
            strategy_version=STRATEGY_VERSION,
            tokenizer_id=provider.tokenizer.id,
            max_pages=get_settings().MAX_PDF_PAGES,
        ).ingest(job, final_attempt=False)
    assert result.outcome.value == "ready", result


class TestTheVerticalSlice:
    async def test_upload_process_and_search_as_two_separate_users(
        self,
        committing_session: Any,
        root: Path,
        collection: QdrantVectorStore,
        embedding_provider: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The API's own provider must be the one the chunks were embedded by,
        # or S4.1's model predicate correctly drops every candidate. In
        # production both processes load the same baked weights; here they
        # share the session-scoped instance so the model loads once.
        from backend.api import composition

        monkeypatch.setattr(
            composition, "FastEmbedProvider", lambda *a, **k: embedding_provider
        )

        # Capture the job the upload enqueues so the worker can run it here.
        # The override must be keyed on the *original* function object, which
        # is what `Depends()` in the route refers to — patching the attribute
        # first would key it on the replacement and silently do nothing. It is
        # set inside the test so it replaces conftest's autouse in-memory
        # queue, exactly as that fixture's docstring says recorders should.
        queue = _CapturingQueue()
        from backend.api.dependencies.services import get_ingestion_queue, get_storage

        app.dependency_overrides[get_ingestion_queue] = lambda: queue
        # The API and the worker must read and write the same bytes. In
        # production both read STORAGE_ROOT; here they share this test's
        # directory, so the upload the API stores is the one the worker verifies
        # against its SHA-256.
        app.dependency_overrides[get_storage] = lambda: LocalStorage(root)

        try:
            async with app.router.lifespan_context(app):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    # 1. Register and log in — two users, real endpoints.
                    alice = await _register_and_login(client, "alice-slice@example.com")
                    bob = await _register_and_login(client, "bob-slice@example.com")

                    # 2. Upload a real PDF.
                    upload = await client.post(
                        "/api/v1/documents/upload",
                        files={
                            "file": ("paper.pdf", make_paper_pdf(), "application/pdf")
                        },
                        headers=_bearer(alice),
                    )
                    assert upload.status_code == 202, upload.text
                    document_id = upload.json()["id"]
                    assert queue.jobs and queue.jobs[0].document_id == document_id
                    assert ingestion_job_id(document_id)

                    # 3. Process it, then poll until the API says `ready`.
                    await _run_the_worker(
                        queue.jobs[0], root, collection, embedding_provider
                    )
                    for _ in range(20):
                        shown = await client.get(
                            f"/api/v1/documents/{document_id}", headers=_bearer(alice)
                        )
                        assert shown.status_code == 200
                        if shown.json()["status"] == "ready":
                            break
                        await asyncio.sleep(0.1)
                    assert shown.json()["status"] == "ready"

                    # 4. Search: at least one result, scored, sectioned, owned.
                    found = await client.post(
                        "/api/v1/search/",
                        json={"query": QUESTION},
                        headers=_bearer(alice),
                    )
                    assert found.status_code == 200, found.text
                    body = found.json()
                    assert body["total"] >= 1
                    for hit in body["results"]:
                        assert hit["document_id"] == document_id
                        assert isinstance(hit["score"], float)
                        assert hit["content"].strip()
                        assert hit["page_start"] >= 1
                        assert hit["embedding_model_id"] == embedding_provider.model_id
                    assert any(
                        hit["section"] for hit in body["results"]
                    ), "at least one hit should resolve to a section"
                    assert [h["score"] for h in body["results"]] == sorted(
                        (h["score"] for h in body["results"]), reverse=True
                    )

                    # 5. The identical search, as the second user: nothing.
                    denied = await client.post(
                        "/api/v1/search/",
                        json={"query": QUESTION},
                        headers=_bearer(bob),
                    )
                    assert denied.status_code == 200, denied.text
                    assert denied.json() == {"results": [], "total": 0}

                    # And naming Alice's document explicitly does not help him.
                    forged = await client.post(
                        "/api/v1/search/",
                        json={"query": QUESTION, "document_ids": [document_id]},
                        headers=_bearer(bob),
                    )
                    assert forged.json() == {"results": [], "total": 0}
        finally:
            app.dependency_overrides.pop(get_ingestion_queue, None)
            app.dependency_overrides.pop(get_storage, None)
