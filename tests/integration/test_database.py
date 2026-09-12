"""Connectivity against a real PostgreSQL.

Marked `db`: skipped when PostgreSQL is unreachable, and required in CI, where
REQUIRE_DB=1 turns that skip into a failure (see tests/conftest.py).

No SQLite substitute. The schema this infrastructure will carry uses JSONB,
UUID and partial indexes, none of which SQLite supports — a passing SQLite test
would say nothing about the code that ships.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from backend.api.dependencies.database import get_db_session
from backend.db.engine import get_engine

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("engine_isolation")]


class TestConnectivity:
    async def test_the_engine_can_execute_a_query(self) -> None:
        """The end-to-end point of this sprint: a round-trip to PostgreSQL."""
        async with get_engine().connect() as connection:
            result = await connection.execute(text("SELECT 1"))

            assert result.scalar_one() == 1

    async def test_the_server_is_postgresql_16_or_newer(self) -> None:
        """Guards the compose pin.

        ADR-0003 specifies PostgreSQL 16. A named volume plus a drifting image
        tag is how a major version changes underneath a project unnoticed.
        """
        async with get_engine().connect() as connection:
            version = (await connection.execute(text("SELECT version()"))).scalar_one()

        assert version.startswith("PostgreSQL "), version
        major = int(version.split()[1].split(".")[0])
        assert major >= 16, f"expected PostgreSQL 16 or newer, got {version}"

    async def test_the_connection_is_to_the_expected_database(self) -> None:
        async with get_engine().connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT current_database(), current_user")
                )
            ).one()

        assert row.current_database == "researchmind"


class TestSessionDependency:
    async def test_it_yields_a_working_session(self) -> None:
        """`get_db_session` is what every future route will depend on."""
        agen = get_db_session()
        session = await anext(agen)
        try:
            result = await session.execute(text("SELECT 42"))

            assert result.scalar_one() == 42
        finally:
            await agen.aclose()

    async def test_it_rolls_back_when_the_caller_raises(self) -> None:
        """A failed request must not leave a transaction open or committed.

        Uses a temporary table so nothing here depends on a schema that does
        not exist until S1.2.
        """
        async with get_engine().connect() as setup:
            await setup.execute(
                text("CREATE TABLE IF NOT EXISTS s11_rollback_probe (id INT)")
            )
            await setup.commit()

        agen = get_db_session()
        session = await anext(agen)
        await session.execute(text("INSERT INTO s11_rollback_probe VALUES (1)"))

        # Drive the dependency's except branch the way FastAPI would.
        with pytest.raises(RuntimeError):
            await agen.athrow(RuntimeError("request failed"))

        try:
            async with get_engine().connect() as check:
                count = (
                    await check.execute(text("SELECT count(*) FROM s11_rollback_probe"))
                ).scalar_one()

            assert count == 0, "the insert was not rolled back"
        finally:
            async with get_engine().connect() as teardown:
                await teardown.execute(text("DROP TABLE s11_rollback_probe"))
                await teardown.commit()

    async def test_it_does_not_commit_on_its_own(self) -> None:
        """Documented behaviour, asserted so it cannot drift.

        ADR-0003 fixes a deletion order spanning PostgreSQL and Qdrant, so the
        service chooses when the PostgreSQL half commits. A dependency that
        committed at teardown would make that sequence unexpressible.
        """
        async with get_engine().connect() as setup:
            await setup.execute(
                text("CREATE TABLE IF NOT EXISTS s11_commit_probe (id INT)")
            )
            await setup.commit()

        agen = get_db_session()
        session = await anext(agen)
        await session.execute(text("INSERT INTO s11_commit_probe VALUES (1)"))
        await agen.aclose()

        try:
            async with get_engine().connect() as check:
                count = (
                    await check.execute(text("SELECT count(*) FROM s11_commit_probe"))
                ).scalar_one()

            assert count == 0, "the dependency committed; it must not"
        finally:
            async with get_engine().connect() as teardown:
                await teardown.execute(text("DROP TABLE s11_commit_probe"))
                await teardown.commit()


class TestFixtureIsolation:
    async def test_the_db_session_fixture_works(self, db_session: AsyncSession) -> None:
        result = await db_session.execute(text("SELECT current_database()"))

        assert result.scalar_one() == "researchmind"

    async def test_a_broken_statement_surfaces_as_a_sqlalchemy_error(
        self, db_session: AsyncSession
    ) -> None:
        """Errors must arrive typed, not as a sentinel a caller might trust."""
        with pytest.raises(SQLAlchemyError):
            await db_session.execute(text("SELECT * FROM a_table_that_is_not_there"))
