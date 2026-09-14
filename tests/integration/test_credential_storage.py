"""Where a password hash is allowed to be, and where it is not.

Against real PostgreSQL through the real repository. The point of these is the
boundary rather than the column: `password_hash` is storage, and the model the
API returns must not carry it. That separation is the reason the project keeps
an ORM model and a domain model that otherwise look alike.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from backend.db.models import UserORM
from backend.db.repositories import UserRepository
from backend.security.passwords import hash_password, verify_password
from shared.models.principal import Principal
from shared.models.user import User, UserRole

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

PASSWORD = "correct-horse-battery"


async def _user(session: Any, email: str) -> User:
    user = await UserRepository(session).create(
        email=email, full_name="Test Researcher", role=UserRole.RESEARCHER
    )
    await session.commit()
    return user


class TestTheColumnAcceptsAHash:
    async def test_a_hash_round_trips_through_postgresql(
        self, committing_session: Any
    ) -> None:
        """Stored, re-read from the database, and still verifies.

        A 60-character `$2b$` string is exactly the kind of value a too-narrow
        column or an encoding mismatch would mangle in a way that only shows up
        at the next sign-in.
        """
        user = await _user(committing_session, "hashed@example.com")
        digest = hash_password(PASSWORD)

        row = await committing_session.get(UserORM, uuid.UUID(user.id))
        row.password_hash = digest
        await committing_session.commit()
        committing_session.expunge_all()

        stored = (
            await committing_session.execute(
                text("SELECT password_hash FROM users WHERE id = :i"),
                {"i": uuid.UUID(user.id)},
            )
        ).scalar_one()

        assert stored == digest
        assert verify_password(PASSWORD, stored) is True

    async def test_the_database_never_holds_the_plaintext(
        self, committing_session: Any
    ) -> None:
        """Asserted across every text column of the row, not just the one."""
        user = await _user(committing_session, "noplain@example.com")
        row = await committing_session.get(UserORM, uuid.UUID(user.id))
        row.password_hash = hash_password(PASSWORD)
        await committing_session.commit()

        whole_row = (
            await committing_session.execute(
                text("SELECT users::text FROM users WHERE id = :i"),
                {"i": uuid.UUID(user.id)},
            )
        ).scalar_one()

        assert PASSWORD not in whole_row

    async def test_a_long_digest_is_not_truncated_by_the_column(
        self, committing_session: Any
    ) -> None:
        """VARCHAR(255) has room for a future cost factor or algorithm prefix.

        PostgreSQL raises on over-length input rather than truncating, so a
        column sized to exactly 60 would turn a cost bump into an outage.
        """
        user = await _user(committing_session, "wide@example.com")
        padded = "$2b$31$" + "x" * 200

        row = await committing_session.get(UserORM, uuid.UUID(user.id))
        row.password_hash = padded
        await committing_session.commit()
        committing_session.expunge_all()

        stored = (
            await committing_session.execute(
                text("SELECT password_hash FROM users WHERE id = :i"),
                {"i": uuid.UUID(user.id)},
            )
        ).scalar_one()

        assert stored == padded


class TestExistingUsersSurviveWithoutCredentials:
    async def test_a_user_created_today_has_no_password(
        self, committing_session: Any
    ) -> None:
        """Nothing writes the column yet — S2.4 adds the only thing that will.

        So this is not a migration artefact to be cleaned up later. It is the
        state of every account in the system.
        """
        user = await _user(committing_session, "nopassword@example.com")

        stored = (
            await committing_session.execute(
                text("SELECT password_hash FROM users WHERE id = :i"),
                {"i": uuid.UUID(user.id)},
            )
        ).scalar_one()

        assert stored is None

    async def test_the_repository_creates_users_without_inventing_one(
        self, committing_session: Any
    ) -> None:
        """`UserRepository.create` takes no password and generates none.

        A default credential would be worse than none: it looks like a password
        somebody chose, and it would be the same one on every row.
        """
        user = await _user(committing_session, "notinvented@example.com")
        row = await committing_session.get(UserORM, uuid.UUID(user.id))

        assert row.password_hash is None

    async def test_an_account_without_a_password_cannot_be_signed_into(
        self, committing_session: Any
    ) -> None:
        """The behaviour that makes a nullable column safe."""
        user = await _user(committing_session, "denied@example.com")
        row = await committing_session.get(UserORM, uuid.UUID(user.id))

        assert verify_password("", row.password_hash) is False
        assert verify_password(PASSWORD, row.password_hash) is False

    async def test_such_a_user_is_still_a_complete_valid_record(
        self, committing_session: Any
    ) -> None:
        """Credential-less is not second-class: they own documents and authenticate.

        S2.2's token authentication resolves a user by id and checks
        `is_active`. It does not consult `password_hash`, so an account with
        NULL there works exactly as it did before this sprint.
        """
        user = await _user(committing_session, "complete@example.com")

        fetched = await UserRepository(committing_session).get_by_id(user.id)

        assert fetched is not None
        assert fetched.email == "complete@example.com"
        assert fetched.is_active is True
        assert fetched.role is UserRole.RESEARCHER


class TestTheHashNeverLeavesPersistence:
    async def test_the_domain_user_has_no_password_field(self) -> None:
        """`User` is the API contract. A hash on it is a hash in a response.

        The ORM/domain split exists so that a column can be storage-only, and
        this is the assertion that makes the split mean something.
        """
        assert "password_hash" not in User.model_fields
        assert not {f for f in User.model_fields if "password" in f}

    async def test_the_repository_returns_a_user_carrying_no_hash(
        self, committing_session: Any
    ) -> None:
        """Behavioural, not structural: the hash is set, and still does not come back."""
        user = await _user(committing_session, "leak@example.com")
        row = await committing_session.get(UserORM, uuid.UUID(user.id))
        row.password_hash = hash_password(PASSWORD)
        await committing_session.commit()

        fetched = await UserRepository(committing_session).get_by_id(user.id)

        assert fetched is not None
        assert not hasattr(fetched, "password_hash")

    async def test_serialising_a_user_cannot_emit_a_hash(
        self, committing_session: Any
    ) -> None:
        """The failure mode this guards is a response body, so serialise one."""
        user = await _user(committing_session, "serialise@example.com")
        row = await committing_session.get(UserORM, uuid.UUID(user.id))
        digest = hash_password(PASSWORD)
        row.password_hash = digest
        await committing_session.commit()

        fetched = await UserRepository(committing_session).get_by_id(user.id)
        assert fetched is not None
        payload = fetched.model_dump_json()

        assert digest not in payload
        assert "password" not in payload.lower()

    async def test_a_principal_carries_no_credential_either(self) -> None:
        """S2.2's authenticated identity is passed down every call path.

        A hash on it would reach the service layer and eventually a log line.
        """
        assert not {f for f in Principal.model_fields if "password" in f}
        assert set(Principal.model_fields) == {"user_id", "email", "role", "is_active"}
