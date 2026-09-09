"""Research workspace endpoints."""
from fastapi import APIRouter, Depends
from backend.security.api_security import get_current_user
from shared.models.user import User

router = APIRouter()


@router.get("/sessions")
async def list_sessions(user: User = Depends(get_current_user)):
    """List research sessions for the current user."""
    ...  # TODO: implement


@router.post("/sessions")
async def create_session(user: User = Depends(get_current_user)):
    """Create a new research session."""
    ...  # TODO: implement
