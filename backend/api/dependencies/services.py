"""Service construction for the API adapter.

These providers exist so that FastAPI's `Depends` stays on this side of the
boundary. ADR-0001 §2 is explicit that "no adapter type (FastAPI `Request`, MCP
`arguments` dict) crosses into the core", and a service whose `__init__`
defaulted a parameter to `Depends(...)` would import FastAPI into
`backend/services/` — making the Service Core unusable from the MCP adapter,
which is the one thing ADR-0001 exists to prevent.

So the wiring is:

    router  --Depends(get_document_service)-->  provider
    provider --Depends(get_db_session)------->  AsyncSession
    provider --DocumentService(session)------>  service
    service  --DocumentRepository(session)--->  repository

One session per request, shared by every repository the service builds from it,
which is what lets several repository operations take part in one transaction.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.database import get_db_session
from backend.config.settings import get_settings
from backend.ingestion import ArqIngestionQueue
from backend.ingestion.queue import redis_settings_from
from backend.api.composition import RetrievalResources, RetrievalUnavailable
from backend.services.auth_service import AuthService
from backend.services.document_service import DocumentService
from backend.services.search_service import SearchService
from backend.storage import LocalStorage
from shared.interfaces.ingestion import IngestionQueue
from shared.interfaces.storage import Storage


def get_storage() -> Storage:
    """The document storage backend (ADR-0008).

    Reads `STORAGE_ROOT` per request rather than at import, so configuration is
    never frozen by the first module that happens to import this one — the same
    rule `backend/db/engine.py` follows. Constructing a `LocalStorage` touches no
    disk.
    """
    return LocalStorage(get_settings().STORAGE_ROOT)


def get_ingestion_queue() -> IngestionQueue:
    """Where accepted uploads are handed off: ARQ over Redis (ADR-0009).

    Swapped from M3/S3.1's deferred queue in M3/S3.2 by changing this function
    and nothing else — the upload service depends on `IngestionQueue`, not on
    ARQ. Reads settings per request, like `get_storage`.
    """
    return ArqIngestionQueue(redis_settings_from(get_settings()))


async def get_document_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[Storage, Depends(get_storage)],
    ingestion: Annotated[IngestionQueue, Depends(get_ingestion_queue)],
) -> DocumentService:
    """Build a DocumentService bound to this request's session."""
    return DocumentService(session, storage=storage, ingestion=ingestion)


async def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AuthService:
    """Build an AuthService bound to this request's session.

    A provider rather than a bare `Depends()` on the class. FastAPI inspects a
    bare-`Depends` class's `__init__` as if its parameters were request fields,
    and an `AsyncSession` is not one — the same `FastAPIError` that forced this
    pattern for `DocumentService` in M1/S1.4.
    """
    return AuthService(session)


def get_retrieval_resources(request: Request) -> RetrievalResources:
    """The provider and vector store this process built at startup.

    Read from `app.state`, never constructed here: one process holds one of
    each for its lifetime (ADR-0014 §11). A missing state attribute means the
    application was built without running its lifespan, which is a wiring bug
    in whoever assembled it — so it raises rather than quietly building a
    second provider and loading the model inside a request.
    """
    resources = getattr(request.app.state, "retrieval", None)
    if not isinstance(resources, RetrievalResources):
        raise RetrievalUnavailable(
            "retrieval resources were never built; the application lifespan did not run"
        )
    return resources


async def get_search_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    resources: Annotated[RetrievalResources, Depends(get_retrieval_resources)],
) -> SearchService:
    """Build a SearchService over this request's session and the process's
    provider and vector store.

    The session is per request — it is a transaction boundary. The provider and
    the vector store are per process. The service is cheap to build and holds
    no state beyond what it is given, so building one per request costs
    nothing and keeps the session's lifetime honest.

    Retrieval settings come from `Settings` per request, for the same reason
    `get_storage` reads `STORAGE_ROOT` per request: configuration read at
    import is configuration a test can no longer override.
    """
    settings = get_settings()
    return SearchService(
        session,
        embedder=resources.embedder,
        vectors=resources.vectors,
        top_k=settings.RETRIEVAL_TOP_K,
        score_threshold=settings.RETRIEVAL_SCORE_THRESHOLD,
        max_query_chars=settings.RETRIEVAL_MAX_QUERY_CHARS,
    )
