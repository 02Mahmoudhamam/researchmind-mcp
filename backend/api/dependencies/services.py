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

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.database import get_db_session
from backend.config.settings import get_settings
from backend.ingestion import DeferredIngestionQueue
from backend.services.auth_service import AuthService
from backend.services.document_service import DocumentService
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
    """Where accepted uploads are handed off (ADR-0009).

    The deferred queue until M3/S3.2 brings the ARQ worker; replacing it is a
    change to this function and nothing else.
    """
    return DeferredIngestionQueue()


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
