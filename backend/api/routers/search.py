"""Semantic search endpoints."""

from fastapi import APIRouter, Depends
from backend.api.schemas.search import SearchRequest, SearchResponse
from backend.api.not_implemented import not_implemented
from backend.services.search_service import SearchService
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from shared.models.principal import Principal

router = APIRouter()


@router.post("/", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest,
    principal: Principal = Depends(require_permission(Permission.SEARCH_QUERY)),
    service: SearchService = Depends(),
) -> SearchResponse:
    """Run semantic search across the user's document corpus — M4."""
    raise not_implemented()
