"""API-level security — the HTTP adapter over the identity resolver.

Everything HTTP-shaped about authentication lives here: header parsing, the
status code, the challenge. The decision itself lives in
``backend.security.authentication``, so the MCP adapter can make the same one
without inheriting FastAPI (ADR-0002 §6).
"""

from typing import Annotated, Awaitable, Callable

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.database import get_db_session
from backend.db.repositories import UserRepository
from backend.security.authentication import AuthenticationError, resolve_principal
from shared.models.principal import Principal
from shared.models.user import UserRole

# `auto_error=False` is the load-bearing argument here.
#
# With the default, HTTPBearer raises on its own: 403 "Not authenticated" for a
# missing header and 403 "Invalid authentication credentials" for a non-Bearer
# scheme. Both were verified against the pre-S2.2 baseline, and both are wrong
# twice over. 403 is the code for an authenticated caller who lacks permission
# (M2/S2.5's job), so returning it for *no* credentials misreports what
# happened; and it settles the request inside a header parser that has not seen
# the token and cannot know anything else about it.
#
# Returning None instead means every authentication failure — absent, malformed,
# forged, expired, unknown subject, deactivated account — is decided in exactly
# one function, which is what principles.md §1 asks for.
security_scheme = HTTPBearer(auto_error=False)

# One message, every failure. Telling a client that the user is unknown rather
# than that the token is bad hands them half the answer; principles.md §7 keeps
# internal detail out of error responses for this reason.
_UNAUTHENTICATED_DETAIL = "Could not validate credentials"


def _unauthenticated() -> HTTPException:
    """The single 401 every authentication failure produces."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHENTICATED_DETAIL,
        # RFC 7235 §4.1: a 401 must state how to authenticate. It is also what
        # tells a client to retry with credentials rather than simply give up.
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(security_scheme)
    ],
) -> Principal:
    """The authenticated identity for this request, or 401.

    Returns a ``Principal`` or raises. There is no path that returns ``None``,
    no ``Optional`` in the signature, and nothing for a route to interpret: by
    the time a handler runs, authentication has already succeeded.

    That is the whole of S2.2. The version this replaces was annotated
    ``-> User`` with a body of ``...``, so it returned ``None`` for every
    request and *every* forged token reached the route.

    The session is request-scoped and comes from the same dependency the rest
    of the application uses (M1/S1.4). Authentication only reads; it never
    commits.
    """
    if credentials is None:
        # No Authorization header, an empty one, empty credentials after the
        # scheme, or a scheme that is not Bearer. HTTPBearer folds these into
        # one None, and they are one answer.
        raise _unauthenticated()

    try:
        return await resolve_principal(credentials.credentials, UserRepository(session))
    except AuthenticationError as exc:
        # `exc.reason` is dropped on purpose. It says which of the failures it
        # was, which is exactly what the client must not learn. It stays on the
        # exception for logs and for the test that asserts the categories are
        # distinguishable internally and identical externally.
        raise _unauthenticated() from exc


def require_role(
    *roles: UserRole,
) -> Callable[..., Awaitable[Principal]]:
    """FastAPI dependency factory — enforces role requirements.

    Still a stub, and deliberately so: authorisation is M2/S2.5. Retyped to
    ``Principal`` here only because ``get_current_user`` no longer returns a
    ``User``, and an annotation that lies is worse than one that is absent.

    When it is implemented it must raise **403**, not 401 — the caller has been
    authenticated by then, and the two codes answer different questions.
    """

    async def _check(
        principal: Principal = Depends(get_current_user),
    ) -> Principal: ...  # TODO: implement — M2/S2.5

    return _check
