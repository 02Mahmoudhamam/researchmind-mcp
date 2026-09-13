"""API-level security middleware and guards."""

from fastapi import Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from shared.models.user import User, UserRole

# The only module-level object left, and it holds no configuration: HTTPBearer
# just parses the Authorization header. The JWTHandler and RBACPolicy instances
# that used to live here were constructed at import, unused by any code path,
# and would have frozen settings for the life of the process. Sprint M2/S2.2
# builds the resolver that needs them, per request.
security_scheme = HTTPBearer()


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
