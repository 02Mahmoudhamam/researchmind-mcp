"""Registration and login.

The first use case in this project that writes a credential, and the first that
issues a token. Both are ordinary service methods: they orchestrate, the
repository owns the SQL, `backend/security/` owns the cryptography, and this is
where the transaction ends (M1/S1.4).

No FastAPI here. ADR-0001 §2 keeps adapter types out of the Service Core, so
these raise domain exceptions and the router turns them into status codes.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import get_settings
from backend.db.repositories import UserRepository
from backend.security.authentication import AuthenticationError
from backend.security.jwt_handler import JWTHandler
from backend.security.passwords import hash_password, verify_password
from shared.models.credentials import AccessToken
from shared.models.user import User

# The constraint migration 0001 put on `users.email`. Named here so that a
# duplicate registration is distinguished from every other integrity failure by
# *which* constraint fired, rather than by guessing from a message.
_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


class EmailAlreadyRegistered(Exception):
    """That email already has an account.

    Not an `AuthenticationError`: nobody is authenticating, and the two must
    not share a response. A failed sign-in has to be indistinguishable from
    every other failed sign-in; a duplicate registration has to be
    distinguishable from a malformed one, or the caller cannot act on it.

    Carries no email. The client supplied it, so echoing it tells them nothing
    new — but it would copy an address into logs and error trackers for no
    benefit.
    """


def _is_duplicate_email(exc: IntegrityError) -> bool:
    """True when this integrity error is the email uniqueness constraint.

    Read from the asyncpg exception's own `constraint_name` rather than matched
    against the message text, so a translated or reworded driver message does
    not turn a duplicate registration into a 500. The string check is a
    fallback for drivers that do not expose the attribute.
    """
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    if name is not None:
        return bool(name == _EMAIL_UNIQUE_CONSTRAINT)
    return _EMAIL_UNIQUE_CONSTRAINT in str(exc.orig)


class AuthService:
    """Account creation and credential authentication.

    Takes a session rather than building one, like every other service here, so
    a caller can compose it into a larger transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._jwt = JWTHandler()

    async def register(self, *, email: str, full_name: str, password: str) -> User:
        """Create an account and return it. **No token is issued.**

        Registration and login are separate operations. Returning a token here
        would mean the account is signed in without anyone having proved they
        can supply the password — which matters the moment registration grows a
        verification step, and costs nothing to get right now.

        The password is hashed *before* it reaches the repository, so plaintext
        never crosses into the persistence layer. `hash_password` validates and
        normalises it (M2/S2.3) and the two are not separable, so a caller that
        skipped the schema still cannot store a password the policy rejects.

        Primitives in, domain model out — the same shape as `DocumentService`.
        A service that took `RegisterRequest` would make `backend/services/`
        depend on `backend/api/`, and the MCP adapter would have to build a REST
        schema to create an account.

        Uniqueness is left to the database. There is no "does this email exist"
        query first: between that check and the insert, another request can
        create the same account, and the only thing that can rule it out is the
        `uq_users_email` constraint doing so atomically. The check-then-insert
        version passes every single-threaded test and fails under load.

        **This is the transaction boundary.** The repository flushes, this
        commits.

        :raises EmailAlreadyRegistered: if the address is taken.
        """
        try:
            user = await self._users.create(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
            )
            await self._session.commit()
            return user
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_email(exc):
                raise EmailAlreadyRegistered() from exc
            raise
        except Exception:
            await self._session.rollback()
            raise

    async def login(self, *, email: str, password: str) -> AccessToken:
        """Verify credentials and issue an access token.

        A read. Nothing is written, so nothing is committed — there is no
        last-login column and no session table to update, and adding a write to
        the sign-in path would make an unauthenticated request able to cause
        one.

        The order below is not the obvious one, and the difference is the
        point. The password is compared **before** the account is known to
        exist and **before** it is known to be active, so that a nonexistent
        account, an account with no password, a wrong password and a disabled
        account all cost one bcrypt round and all raise the same error.
        Checking existence first would let an attacker distinguish registered
        addresses by timing alone, with every response body still identical.

        `verify_password` performs that throwaway round itself when handed no
        hash, so this method cannot forget to.

        :raises AuthenticationError: on any failure, with no partial result.
        """
        credentials = await self._users.get_credentials_by_email(email)

        # Evaluated before the branch, never inside it: `credentials is None or
        # not verify_password(...)` would short-circuit and skip the bcrypt
        # round for exactly the case that needs it most.
        stored = credentials.password_hash if credentials is not None else None
        matched = verify_password(password, stored)

        if credentials is None or not matched:
            raise AuthenticationError("email or password does not match")

        if not credentials.is_active:
            # After the password check, so a disabled account costs the same as
            # a wrong one. The same rule S2.2 applies to tokens, applied here to
            # credentials: `resolve_principal` would reject this user on the
            # next request anyway, so issuing a token would produce one that
            # cannot be used.
            raise AuthenticationError("user is not active")

        settings = get_settings()
        return AccessToken(
            token=self._jwt.create_access_token(
                credentials.user_id, credentials.email, credentials.role
            ),
            # Read from the same settings object the handler reads, so the
            # advertised lifetime and the `exp` inside the token cannot
            # disagree.
            expires_in_seconds=settings.JWT_EXPIRE_MINUTES * 60,
        )
