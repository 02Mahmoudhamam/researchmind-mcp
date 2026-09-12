"""Semantic search endpoints."""

from fastapi import APIRouter, Depends
from backend.api.schemas.search import SearchRequest, SearchResponse
from backend.services.search_service import SearchService
from backend.security.api_security import get_current_user
from shared.models.user import User

router = APIRouter()


@router.post("/", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest,
    user: User = Depends(get_current_user),
    service: SearchService = Depends(),
):
    """Run semantic search across the user's document corpus."""
    ...  # TODO: implement
