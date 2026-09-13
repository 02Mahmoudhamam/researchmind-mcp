"""JWT access-token creation and verification.

Access tokens only. There is no refresh token and no server-side revocation —
`docs/security/principles.md` §6 states that plainly rather than implying a
safety net that does not exist, which is also why the TTL is short.

Nothing here touches the database. Resolving a token's subject to an active
user is authentication, and it belongs to Sprint M2/S2.2.
"""

from datetime import datetime, timedelta, timezone

import jwt

from backend.config.settings import Settings, get_settings
from shared.models.user import TokenData, UserRole


class TokenError(Exception):
    """A token could not be verified.

    One exception type, not a hierarchy. Callers need exactly one decision —
    trust this token or reject it — and `principles.md` §1 requires the failure
    to be a raise rather than a sentinel a caller might treat as success.

    It also keeps PyJWT out of caller imports, so swapping the library again
    would not ripple outward.
    """


class JWTHandler:
    """Signs and verifies access tokens."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Settings are resolved lazily, never captured at import.

        The optional override exists so a test can sign with one secret and
        verify with another without mutating process environment. Production
        passes nothing and picks up the cached settings at call time.
        """
        self._override = settings

    @property
    def _settings(self) -> Settings:
        return self._override or get_settings()

    def create_access_token(
        self,
        user_id: str,
        email: str,
        role: UserRole,
        expires_delta: timedelta | None = None,
    ) -> str:
        """Return a signed access token for this user.

        Claims are deliberately minimal — subject, email, role, expiry. No
        `iat`, no `jti`: there is no revocation store to compare them against,
        and a claim nothing reads is a claim that will drift out of step with
        whatever eventually does read it.

        `exp` is always present. A token without one cannot expire, and
        `verify_token` refuses to accept it.
        """
        settings = self._settings
        expires = expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
        payload = {
            # `sub` rather than a custom claim: it is the registered subject
            # claim, so PyJWT and every other JWT tool agree on its meaning.
            "sub": user_id,
            "email": email,
            "role": role.value,
            "exp": datetime.now(timezone.utc) + expires,
        }
        return jwt.encode(
            payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
        )

    def verify_token(self, token: str) -> TokenData:
        """Verify a token and return its claims, or raise TokenError.

        There is no failure path that returns a value. Every rejection —
        bad signature, expired, tampered, malformed, missing claim, wrong
        algorithm — leaves by the same raise.

        `algorithms` is passed explicitly and comes from configuration, which is
        bounded to HMAC by a validator on the setting. It is never read from the
        token's own header: a token that says `{"alg": "none"}` is asking not to
        be verified, and a token that says `RS256` against an HMAC secret is the
        public-key-as-secret forgery. PyJWT refuses both because the accepted
        list is supplied by us.

        `require` makes `exp` and `sub` structural. Without it PyJWT happily
        accepts a token carrying neither, and a token with no expiry never
        stops being valid.
        """
        settings = self._settings
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=[settings.JWT_ALGORITHM],
                options={"require": ["exp", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            # The message is PyJWT's and says what was wrong with the token, not
            # what the secret is.
            raise TokenError(str(exc)) from exc

        try:
            return TokenData(
                user_id=payload["sub"],
                email=payload["email"],
                role=UserRole(payload["role"]),
                exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            )
        except (KeyError, ValueError) as exc:
            # A correctly signed token can still carry claims this application
            # cannot use — an unknown role, a missing email. Signed by us is not
            # the same as meaningful to us, and the safe answer is the same one.
            raise TokenError(f"token claims are not usable: {exc}") from exc
