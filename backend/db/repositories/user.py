"""Data access for users.

`User` is the ownership root: it is not owned by anything, so ID-only lookup is
legitimate here in a way it is not for documents or chunks. See the module
docstring in `document.py` for the contrast.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import UserORM
from backend.db.repositories._identifiers import parse_id
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

    No authentication of any kind: no password handling, no credential
    verification, no session issuing. Those are Milestone M2, and the schema
    has no column for them.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        email: str,
        full_name: str,
        role: UserRole = UserRole.RESEARCHER,
    ) -> User:
        """Insert a user and return it.

        Flushes but does not commit — the caller owns the transaction. A
        duplicate email surfaces as the IntegrityError PostgreSQL raises; this
        does not pre-check, because a check followed by an insert is a race.
        """
        row = UserORM(email=email, full_name=full_name, role=role)
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
        """Look a user up by email — the login path M2 will use."""
        result = await self._session.execute(
            select(UserORM).where(UserORM.email == email)
        )
        row = result.scalar_one_or_none()
        return None if row is None else _to_domain(row)
