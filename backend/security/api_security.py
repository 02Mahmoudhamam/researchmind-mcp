"""API-level security — the HTTP adapter over authentication and authorisation.

Everything HTTP-shaped about both lives here: header parsing, status codes, the
challenge. The decisions live elsewhere — identity in
``backend.security.authentication``, permission in ``backend.security.rbac`` —
so the MCP adapter can make the same decisions without inheriting FastAPI
(ADR-0002 §6).

The two status codes answer two different questions and must never be swapped:

    401  we do not know who you are      authentication failed; retry with credentials
    403  we know, and you may not        authenticated, but not permitted

A 403 is only ever raised *after* ``get_current_user`` has returned a Principal,
because every authorisation dependency below depends on it. There is no path to
a 403 without first passing authentication, and so no way for a missing or
forged token to be reported as a permissions problem.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.database import get_db_session
from backend.db.repositories import UserRepository
from backend.security.authentication import AuthenticationError, resolve_principal
from backend.security.rbac import Permission, RBACPolicy
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


def unauthenticated() -> HTTPException:
    """The single 401 every authentication failure produces.

    Public since M2/S2.4, so the login route raises the *same object shape* a
    forged bearer token raises. Two helpers would have been two chances for the
    detail string or the challenge header to diverge, and the whole point is
    that a failed sign-in and a failed token are indistinguishable from
    outside.
    """
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
        raise unauthenticated()

    try:
        return await resolve_principal(credentials.credentials, UserRepository(session))
    except AuthenticationError as exc:
        # `exc.reason` is dropped on purpose. It says which of the failures it
        # was, which is exactly what the client must not learn. It stays on the
        # exception for logs and for the test that asserts the categories are
        # distinguishable internally and identical externally.
        raise unauthenticated() from exc


# One message for every refusal. It says access was refused and nothing about
# why: naming the permission that was missing, or the role that would have had
# it, describes the policy to anyone probing it (principles.md §7).
_FORBIDDEN_DETAIL = "You do not have permission to perform this action."


def forbidden() -> HTTPException:
    """The single 403 every authorisation failure produces.

    No ``WWW-Authenticate`` header, deliberately. That header invites the client
    to authenticate, and this caller already has — sending different
    credentials is not the remedy, and the header would suggest it is.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN_DETAIL
    )


class RequirePermission:
    """Dependency: the authenticated Principal, if its role holds a permission.

    This is how routes are authorised. A route declares *what it does* — read a
    document, run an agent — and the policy decides which roles may do that, in
    one place. The alternative, a role list at every route, would copy the
    matrix in ``rbac.py`` into nine routers, and the copies would drift the
    first time the matrix changed.

    A class rather than a closure so the permission a route requires is
    readable from the route itself. The route-coverage test walks the
    application's dependency tree, finds these, and checks every protected
    route against the expected matrix — which a closure named ``_check`` would
    not let it do.
    """

    def __init__(self, permission: Permission) -> None:
        if not isinstance(permission, Permission):
            # At import, when a router is defined — not at the first request.
            raise TypeError(f"expected a Permission, got {type(permission).__name__}")
        self.permission = permission
        self._policy = RBACPolicy()

    async def __call__(
        self, principal: Annotated[Principal, Depends(get_current_user)]
    ) -> Principal:
        # `principal.role` is the database's current answer, resolved on this
        # request by `resolve_principal`. The token's own role claim never
        # reaches this line, so a demotion takes effect on the next request
        # rather than at token expiry.
        if not self._policy.has_permission(principal.role, self.permission):
            raise forbidden()
        return principal


class RequireRole:
    """Dependency: the authenticated Principal, if its role is one of these.

    For the rare check that genuinely is about *who* rather than *what* — an
    administrative operation that no permission describes. No route in the
    application needs one today: all nine protected routes are authorised by
    permission. It exists because the scaffold declared it and because the
    first administrative route should not have to invent it.
    """

    def __init__(self, roles: frozenset[UserRole]) -> None:
        if not roles:
            # An empty set would refuse everyone, which fails closed but is
            # certainly a mistake — better reported when the router is defined.
            raise ValueError("require_role needs at least one role")
        if not all(isinstance(role, UserRole) for role in roles):
            raise TypeError("require_role accepts UserRole members only")
        self.roles = roles

    async def __call__(
        self, principal: Annotated[Principal, Depends(get_current_user)]
    ) -> Principal:
        if principal.role not in self.roles:
            raise forbidden()
        return principal


def require_permission(permission: Permission) -> RequirePermission:
    """FastAPI dependency factory — the permission check routes use.

    ``principal: Principal = Depends(require_permission(Permission.DOCUMENT_READ))``

    No token or a bad one: 401, from ``get_current_user``, before the policy is
    consulted. Authenticated without the permission: 403. Otherwise the
    Principal, exactly as authentication produced it.
    """
    return RequirePermission(permission)


def require_role(*roles: UserRole) -> RequireRole:
    """FastAPI dependency factory — enforces role requirements.

    Same 401/403 contract as ``require_permission``. Prefer that one: a role
    check hard-codes today's answer to "who may do this" at the call site.
    """
    return RequireRole(frozenset(roles))
