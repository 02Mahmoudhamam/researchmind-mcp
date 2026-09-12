"""Database infrastructure that must hold without a database present.

These tests exist because each one guards a specific way this layer could be
wrong while still looking correct.
"""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.db.base import NAMING_CONVENTION, Base


def _settings(**overrides: object) -> Settings:
    """Build Settings with the required fields filled in."""
    base: dict[str, object] = {
        "ANTHROPIC_API_KEY": "test-key",
        **overrides,
    }
    return Settings(**base)  # type: ignore[arg-type]


class TestConfiguration:
    def test_a_synchronous_dsn_is_rejected(self) -> None:
        """`postgresql://` must fail in the config layer, not inside the engine.

        It is the form every connection string and tutorial uses, and it
        resolves to psycopg2 — not installed, and not async.
        """
        with pytest.raises(ValidationError) as excinfo:
            _settings(DATABASE_URL="postgresql://u:p@localhost:5432/db")

        message = str(excinfo.value)
        assert "asyncpg" in message, "the error must name the driver required"
        assert "postgresql://" in message, "the error must show what was given"

    def test_an_asyncpg_dsn_is_accepted(self) -> None:
        settings = _settings(DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db")
        assert settings.DATABASE_URL.startswith("postgresql+asyncpg://")

    def test_the_default_dsn_is_usable_as_shipped(self) -> None:
        """The default must satisfy the validator it is shipped alongside."""
        assert _settings().DATABASE_URL.startswith("postgresql+asyncpg://")


class TestLazyEngine:
    def test_importing_the_db_package_does_not_touch_the_database(self) -> None:
        """Import must be side-effect free, proven against an unroutable host.

        `vector_db/qdrant/config.py` and `memory_system/redis/config.py` call
        `get_settings()` at module scope and freeze it into class attributes. An
        engine built the same way would also allocate a connection pool at
        import, so importing the package would require a reachable database and
        test collection would fail on any machine without one.

        Run in a subprocess so this process's already-imported modules and
        settings cache cannot mask the result.
        """
        script = (
            "import backend.db as db\n"
            "assert db.get_engine.cache_info().currsize == 0, "
            "'importing backend.db built an engine'\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "ANTHROPIC_API_KEY": "test-key",
                # Unroutable by RFC 5737. If import connects, this hangs or
                # fails rather than silently succeeding against a local server.
                "DATABASE_URL": "postgresql+asyncpg://u:p@192.0.2.1:5432/db",
            },
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

    def test_get_engine_is_cached(self) -> None:
        from backend.db.engine import get_engine

        assert get_engine() is get_engine()


class TestSessionFactory:
    def test_sessions_do_not_expire_on_commit(self) -> None:
        """Attributes must stay readable after a commit.

        With the default `expire_on_commit=True`, SQLAlchemy expires every
        attribute on commit and reloads it on next access. Under asyncio that
        reload is I/O outside an await and raises `MissingGreenlet`, naming
        neither the attribute nor the commit. The repository layer maps ORM rows
        to Pydantic models after committing, so every such read would trip it.
        """
        from backend.db.session import get_sessionmaker

        assert get_sessionmaker().kw["expire_on_commit"] is False


class TestSchemaOwnership:
    def test_the_application_never_creates_schema_itself(self) -> None:
        """Schema lifecycle belongs to Alembic, not to application startup.

        `Base.metadata.create_all()` is the tempting shortcut: it makes tests
        and local runs work without migrations, and it means the migration that
        ships to production is the one thing nobody exercised. It also races
        between replicas at boot.

        Verified separately by running the app against an empty database: it
        imports, completes its lifespan, and creates nothing.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        searched = [root / "main.py", *(root / "backend").rglob("*.py")]
        offenders = [
            str(path.relative_to(root))
            for path in searched
            if "create_all" in path.read_text(encoding="utf-8")
            or "drop_all" in path.read_text(encoding="utf-8")
        ]

        assert not offenders, f"schema creation leaked into the app: {offenders}"


class TestNamingConvention:
    def test_every_constraint_kind_is_named(self) -> None:
        """All five kinds must be templated before the first migration.

        A kind left out gets a PostgreSQL-assigned name that SQLAlchemy cannot
        reference, so a generated `downgrade()` has no name to drop it by.
        """
        assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}

    def test_the_convention_is_attached_to_the_metadata(self) -> None:
        assert Base.metadata.naming_convention == NAMING_CONVENTION

    def test_multi_column_indexes_cannot_collide(self) -> None:
        """`column_0_N_name`, not `column_0_label`.

        `documents` is planned to carry both (user_id, created_at) and
        (user_id, content_hash). Under the label form both would be named
        `ix_documents_user_id` and the second would fail to create.
        """
        assert "column_0_N_name" in NAMING_CONVENTION["ix"]
        assert "column_0_N_name" in NAMING_CONVENTION["uq"]
