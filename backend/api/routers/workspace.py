"""Research workspace endpoints."""

from fastapi import APIRouter, Depends
from backend.api.not_implemented import not_implemented
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from shared.models.principal import Principal

router = APIRouter()


@router.get("/sessions")
async def list_sessions(
    principal: Principal = Depends(require_permission(Permission.WORKSPACE_READ)),
) -> None:
    """List research sessions for the current user — not yet scheduled."""
    raise not_implemented()


@router.post("/sessions")
async def create_session(
    principal: Principal = Depends(require_permission(Permission.WORKSPACE_WRITE)),
) -> None:
    """Create a new research session — not yet scheduled."""
    raise not_implemented()
