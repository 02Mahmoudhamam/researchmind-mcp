"""Migration lifecycle against real PostgreSQL.

Runs the `alembic` command itself rather than calling into its API, because the
command is what a developer and a deployment actually invoke. Exit codes alone
are not trusted: each step inspects `information_schema` and `pg_catalog` for
what the database really contains.

These tests mutate the schema — they downgrade to empty and back — so they
restore head before finishing.
"""

import subprocess
import sys
from typing import Any

import pytest
from sqlalchemy import text

from backend.db.engine import get_engine

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

EXPECTED_TABLES = {"users", "documents", "document_chunks"}

EXPECTED_CONSTRAINTS = {
    "pk_users",
    "uq_users_email",
    "ck_users_user_role",
    "pk_documents",
    "fk_documents_user_id_users",
    "ck_documents_chunk_count_non_negative",
    "ck_documents_document_type",
    "ck_documents_document_status",
    "pk_document_chunks",
    "fk_document_chunks_document_id_documents",
    "uq_document_chunks_document_id_chunk_index",
    "ck_document_chunks_chunk_index_non_negative",
    "ck_document_chunks_dimension_positive",
}


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the alembic CLI the way a developer would."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        timeout=180,
    )


async def _scalar(sql: str) -> Any:
    async with get_engine().connect() as connection:
        return (await connection.execute(text(sql))).scalar_one()


async def _names(sql: str) -> set[str]:
    async with get_engine().connect() as connection:
        return {row[0] for row in (await connection.execute(text(sql))).all()}


TABLES_SQL = (
    "SELECT tablename FROM pg_tables "
    "WHERE schemaname='public' AND tablename <> 'alembic_version'"
)
CONSTRAINTS_SQL = (
    "SELECT conname FROM pg_constraint "
    "WHERE connamespace='public'::regnamespace "
    "AND conrelid::regclass::text <> 'alembic_version'"
)
INDEXES_SQL = (
    "SELECT indexname FROM pg_indexes "
    "WHERE schemaname='public' AND tablename <> 'alembic_version'"
)
ENUM_TYPES_SQL = (
    "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname='public' AND t.typtype='e'"
)


class TestSchemaProducedByMigration:
    async def test_the_expected_tables_exist(self) -> None:
        assert await _names(TABLES_SQL) == EXPECTED_TABLES

    async def test_every_constraint_is_present_and_conventionally_named(self) -> None:
        """Names are asserted, not just counted.

        An auto-named constraint is one `downgrade()` cannot drop, which is how
        a migration stops being reversible.
        """
        assert await _names(CONSTRAINTS_SQL) == EXPECTED_CONSTRAINTS

    async def test_the_documents_index_is_partial_on_not_deleted(self) -> None:
        """The predicate is the point: soft-deleted rows are never listed."""
        definition = await _scalar(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_documents_user_id_created_at'"
        )

        assert "(user_id, created_at)" in definition
        assert "WHERE (deleted_at IS NULL)" in definition

    async def test_foreign_key_delete_actions_are_as_designed(self) -> None:
        """'r' is RESTRICT, 'c' is CASCADE — read from pg_catalog, not the ORM."""
        async with get_engine().connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        # ::text because confdeltype is PostgreSQL's internal
                        # "char" type, which asyncpg hands back as bytes.
                        "SELECT conname, confdeltype::text FROM pg_constraint "
                        "WHERE contype = 'f'"
                    )
                )
            ).all()
        actions = {name: action for name, action in rows}

        assert actions["fk_documents_user_id_users"] == "r"
        assert actions["fk_document_chunks_document_id_documents"] == "c"

    async def test_no_native_enum_types_were_created(self) -> None:
        """A native enum survives `drop_table` and breaks the downgrade.

        The columns are VARCHAR with a CHECK for exactly this reason.
        """
        assert await _scalar(ENUM_TYPES_SQL) == 0

    async def test_the_only_credential_column_is_the_password_hash(self) -> None:
        """Asserted against the live schema, not only the model.

        Migration 0002 introduced exactly one credential column. This is the
        database's own account of that, read from information_schema rather
        than from the ORM that was supposed to produce it.
        """
        columns = await _names(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public'"
        )
        credential_like = {
            "password",
            "passwd",
            "password_hash",
            "hashed_password",
            "salt",
            "secret",
            "api_key",
            "refresh_token",
        }

        assert columns & credential_like == {"password_hash"}

    async def test_the_migrated_password_hash_column_is_nullable(self) -> None:
        """The property that makes 0002 safe on a table that already has rows."""
        nullable = await _scalar(
            "SELECT is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'password_hash'"
        )

        assert nullable is True

    async def test_the_password_hash_column_has_no_default(self) -> None:
        """A default would silently give every existing row a credential.

        It would also make ADD COLUMN rewrite the table on older PostgreSQL.
        Both are reasons the column arrives empty and stays that way until
        S2.4 writes to it.
        """
        default = await _scalar(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'password_hash'"
        )

        assert default is None


class TestModelSchemaAgreement:
    def test_alembic_check_reports_no_drift(self) -> None:
        """The models and the migrated schema describe the same thing.

        `compare_type` and `compare_server_default` are enabled in env.py, so
        this also catches a changed column type or default — not just a missing
        table.
        """
        result = alembic("check")

        assert result.returncode == 0, result.stderr
        assert "No new upgrade operations detected" in (result.stdout + result.stderr)


class TestLifecycle:
    async def test_downgrade_then_upgrade_restores_the_schema_exactly(self) -> None:
        """upgrade -> downgrade -> upgrade, inspecting the database each time.

        A migration that cannot round-trip is not reviewable: there is no way to
        try it, back it out, and try again.
        """
        before_tables = await _names(TABLES_SQL)
        before_constraints = await _names(CONSTRAINTS_SQL)
        before_indexes = await _names(INDEXES_SQL)
        assert before_tables == EXPECTED_TABLES

        try:
            down = alembic("downgrade", "base")
            assert down.returncode == 0, down.stderr

            assert await _names(TABLES_SQL) == set(), "tables survived the downgrade"
            assert await _names(CONSTRAINTS_SQL) == set()
            assert await _names(INDEXES_SQL) == set()
            assert await _scalar(ENUM_TYPES_SQL) == 0, "an enum type was orphaned"
            assert await _scalar("SELECT count(*) FROM alembic_version") == 0
        finally:
            up = alembic("upgrade", "head")
            assert up.returncode == 0, up.stderr

        assert await _names(TABLES_SQL) == before_tables
        assert await _names(CONSTRAINTS_SQL) == before_constraints
        assert await _names(INDEXES_SQL) == before_indexes

    def test_the_head_revision_is_the_expected_one(self) -> None:
        """A single linear history; no accidental branch."""
        result = alembic("heads")

        assert result.returncode == 0, result.stderr
        assert "0002" in result.stdout
        assert result.stdout.count("(head)") == 1, "more than one head — branched"
