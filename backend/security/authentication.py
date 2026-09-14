"""Resolving a bearer token to an authenticated identity.

This is the *shared* resolver ADR-0002 §6 requires — "Both adapters resolve
identity through the same resolver, so MCP cannot become a path around REST
authentication." It therefore imports no FastAPI and knows nothing about HTTP.
``backend/security/api_security.py`` is the thin HTTP adapter over it, and the
MCP adapter can call this same function when that milestone arrives, without
either inheriting the other's error handling.

Nothing here returns a value that means "unauthenticated". Every failure is a
raise (``docs/security/principles.md`` §1).
"""

from backend.db.repositories import UserRepository
from backend.security.jwt_handler import JWTHandler, TokenError
from shared.models.principal import Principal


class AuthenticationError(Exception):
    """Authentication failed. Why it failed is internal.

    One exception type rather than a hierarchy: the caller's decision is
    binary, and separate classes invite a handler that treats one branch as
    recoverable — which is how a fail-closed rule stops being one.

    ``reason`` records what actually happened, for logs and for tests. It must
    never reach a client. An error that distinguishes "no such user" from "that
    account is disabled" is an account-enumeration oracle, and principles.md §7
    requires error responses to carry no internal detail. The HTTP adapter maps
    every instance of this to one identical 401.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def resolve_principal(token: str, users: UserRepository) -> Principal:
    """Verify ``token`` and return the identity it authenticates, or raise.

    The order matters. Signature verification comes first, so a forged or
    malformed token never reaches the database: otherwise anyone could drive
    user lookups with arbitrary subjects at the cost of one unsigned string.

    :raises AuthenticationError: on any failure, with no partial result.
    """
    try:
        claims = JWTHandler().verify_token(token)
    except TokenError as exc:
        # Every token-level rejection — bad signature, expired, tampered,
        # `alg: none`, missing `exp` or `sub`, an unknown role — arrives here as
        # one category. The distinction mattered to `verify_token`; it does not
        # matter to the caller, who either has an identity or does not.
        raise AuthenticationError(f"token rejected: {exc}") from exc

    user = await users.get_by_id(claims.user_id)
    if user is None:
        # Covers a subject that never existed, one deleted after the token was
        # issued, and a `sub` that is not a UUID at all — `parse_id` returns
        # None for that last case rather than raising, so a token claiming
        # `sub: "../../etc/passwd"` is an ordinary 401 and not a 500.
        raise AuthenticationError("token subject does not resolve to a user")

    if not user.is_active:
        # A valid signature proves when the token was minted, not that the
        # account is still in service. With no revocation store in the MVP
        # (principles.md §6) this lookup is the only thing that can end a
        # session before its expiry — which is the entire reason authentication
        # reads the database instead of trusting claims it just verified.
        raise AuthenticationError("user is not active")

    # Role and email come from the row, never from the token.
    #
    # A token issued before a demotion still carries the old role. Honouring it
    # would leave an ex-administrator privileged until expiry, for exactly the
    # reason honouring a deactivated user's token would leave them signed in.
    # The token answers "who do you claim to be"; PostgreSQL — the sole
    # authority, principles.md §3 — answers "and what are you now".
    #
    # `is_active` is passed as the literal True rather than `user.is_active`,
    # because the guard above is what establishes it. See `Principal`.
    return Principal(
        user_id=user.id,
        email=user.email,
        role=user.role,
        is_active=True,
    )
