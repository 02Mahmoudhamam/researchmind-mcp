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
from backend.services.document_service import DocumentService


async def get_document_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentService:
    """Build a DocumentService bound to this request's session."""
    return DocumentService(session)
