"""Data access for users.

`User` is the ownership root: it is not owned by anything, so ID-only lookup is
legitimate here in a way it is not for documents or chunks. See the module
docstring in `document.py` for the contrast.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import UserORM
from backend.db.repositories._identifiers import parse_id
from shared.models.credentials import UserCredentials
from shared.models.user import User, UserRole


def _to_domain(row: UserORM) -> User:
    """Map the ORM row to the API contract.

    The mapping is explicit and lives here so that no ORM object escapes the
    repository. A caller holding a detached ORM instance would hit lazy-load
    errors far from the cause; a Pydantic model has no such surprises.
    """
    return User(
        id=str(row.id),
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        is_active=row.is_active,
        created_at=row.created_at,
    )


class UserRepository:
    """Users, by id or by email.

    Two return types, and the difference is the security boundary. Every method
    that answers "who is this" returns `User`, which has no credential on it at
    all. `get_credentials_by_email` is the single method that reads
    `password_hash`, and it returns a type built for that one purpose.

    No verification happens here — comparing a password is
    `backend/security/passwords.py`, and issuing a token is the service. This
    reads rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        email: str,
        full_name: str,
        role: UserRole = UserRole.RESEARCHER,
        password_hash: str | None = None,
    ) -> User:
        """Insert a user and return it.

        Flushes but does not commit — the caller owns the transaction. A
        duplicate email surfaces as the IntegrityError PostgreSQL raises; this
        does not pre-check, because a check followed by an insert is a race.

        `password_hash` is a **bcrypt digest**, never a password. The hashing
        happens in `backend/security/passwords.py` before this is called, so a
        plaintext value never reaches the persistence layer at all. It defaults
        to None because an account without one is legitimate: that is every row
        created before M2/S2.4, and `verify_password` refuses them.

        The returned `User` does not carry the hash back out.
        """
        row = UserORM(
            email=email,
            full_name=full_name,
            role=role,
            password_hash=password_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get_by_id(self, user_id: str) -> User | None:
        """Look a user up by id.

        ID-only, and safe: a user is not a user-owned resource. Milestone M2's
        `get_current_user` resolves a verified token subject through here.
        """
        parsed = parse_id(user_id)
        if parsed is None:
            return None
        row = await self._session.get(UserORM, parsed)
        return None if row is None else _to_domain(row)

    async def get_by_email(self, email: str) -> User | None:
        """Look a user up by email. No credential comes back."""
        result = await self._session.execute(
            select(UserORM).where(UserORM.email == email)
        )
        row = result.scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def get_credentials_by_email(self, email: str) -> UserCredentials | None:
        """Read what is needed to verify a sign-in attempt, and only that.

        The one method in this package that returns `password_hash`. Separate
        from `get_by_email` rather than a flag on it, because a flag makes the
        unsafe result reachable from every existing call site — and the callers
        that want identity vastly outnumber the one that wants a credential.

        Deliberately **not** filtered on `is_active`. Skipping inactive accounts
        here would return None for them, making a disabled account answer in the
        same time as a nonexistent one and a *different* time from a live one —
        the enumeration gap `verify_password` closes. The service checks
        `is_active` after the password comparison, so every failing sign-in
        costs the same.

        Selects the five columns authentication uses rather than the whole row,
        so a column added later does not silently join the credential path.
        """
        result = await self._session.execute(
            select(
                UserORM.id,
                UserORM.email,
                UserORM.password_hash,
                UserORM.role,
                UserORM.is_active,
            ).where(UserORM.email == email)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return UserCredentials(
            user_id=str(row.id),
            email=row.email,
            password_hash=row.password_hash,
            role=row.role,
            is_active=row.is_active,
        )
