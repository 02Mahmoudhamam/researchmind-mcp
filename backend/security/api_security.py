"""API-level security middleware and guards."""

from fastapi import Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from shared.models.user import User, UserRole
from backend.security.jwt_handler import JWTHandler
from backend.security.rbac import RBACPolicy

security_scheme = HTTPBearer()
jwt_handler = JWTHandler()
rbac = RBACPolicy()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
) -> User:
    """FastAPI dependency — extracts and validates the current user."""
    ...  # TODO: implement


def require_role(*roles: UserRole):
    """FastAPI dependency factory — enforces role requirements."""

    async def _check(
        user: User = Depends(get_current_user),
    ) -> User: ...  # TODO: implement

    return _check
