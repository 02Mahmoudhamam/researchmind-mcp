"""Semantic search endpoints."""

from fastapi import APIRouter, Depends
from backend.api.schemas.search import SearchRequest, SearchResponse
from backend.api.not_implemented import not_implemented
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from shared.models.principal import Principal

router = APIRouter()


@router.post("/", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest,
    principal: Principal = Depends(require_permission(Permission.SEARCH_QUERY)),
) -> SearchResponse:
    """Run semantic search across the user's document corpus.

    Still 501. `SearchService` exists and works as of M4/S4.1, but exposing it
    here means deciding whether the API process loads the embedding model and
    holds a Qdrant connection pool — the API's `lifespan` deliberately does
    nothing, and ADR-0004/0005/0013 put both in the worker. ADR-0014 §9 records
    that decision as the next M4 sprint's, not this one's.

    The `SearchService = Depends()` parameter that used to sit here was
    removed with it: the service now takes a session and its collaborators, so
    FastAPI could not construct one, and nothing on a route that raises should
    pretend otherwise.
    """
    raise not_implemented()
