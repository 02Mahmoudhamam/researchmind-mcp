"""Pytest configuration shared by the whole suite.

Establishes a deterministic settings environment *before* any test module is
imported, so a test run never depends on whatever ``.env`` happens to exist on
the developer's machine.

Why this is done at import time rather than in a fixture
--------------------------------------------------------
pytest imports ``conftest.py`` before it collects test modules, and several
modules call ``get_settings()`` at import time — ``backend/api/app.py:7`` and
``backend/security/jwt_handler.py:8`` among them. By the time a fixture ran,
those imports would already have happened against the developer's environment.

Values are set unconditionally rather than with ``setdefault``. That guarantees
the same configuration on every machine and in CI, and it means a real
``ANTHROPIC_API_KEY`` present in the shell cannot leak into a test run.
"""

import os
from typing import Any, AsyncIterator

_TEST_ENV: dict[str, str] = {
    # Required by Settings and has no default; without it nothing imports.
    "ANTHROPIC_API_KEY": "test-anthropic-api-key",
    # Placeholders. Sprint M2/S2.1 makes the application refuse to start on the
    # "changeme" defaults outside development, so tests set explicit values.
    "SECRET_KEY": "test-secret-key",
    "JWT_SECRET": "test-jwt-secret",
    "APP_ENV": "test",
    "DEBUG": "false",
    "LOG_LEVEL": "WARNING",
}

for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value

# DATABASE_URL is the one setting deliberately NOT forced.
#
# Everything above is set unconditionally so a real ANTHROPIC_API_KEY in the
# shell cannot leak into a test run. For the database the requirement inverts:
# tests marked `db` need a *genuinely reachable* PostgreSQL, so an operator
# pointing at their own instance — or CI pointing at a service container — must
# win. `setdefault` keeps the run deterministic for everyone else by falling
# back to the credentials docker-compose.yml declares.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://researchmind:researchmind@localhost:5432/researchmind",
)


import pytest  # noqa: E402  (must follow the environment setup above)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the ``get_settings`` LRU cache around every test.

    ``get_settings`` is ``@lru_cache()``d, so a test that alters configuration
    would otherwise leak that state into every test that follows.
    """
    from backend.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def api_client():
    """An httpx client bound directly to the ASGI application.

    Uses ``ASGITransport`` rather than the ``AsyncClient(app=...)`` shortcut,
    which httpx deprecated in 0.27 and removed in 0.28. Requests are dispatched
    in-process against the real application — routing, middleware, dependency
    injection and response validation all execute — so no socket is opened and
    no network is required. That keeps API tests deterministic and makes them
    exercise the application rather than a stand-in for it.
    """
    from httpx import ASGITransport, AsyncClient

    from backend.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


def _database_is_reachable() -> bool:
    """Open a real TCP connection to the configured database, once per run."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(os.environ["DATABASE_URL"])
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 5432), 2.0):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: object, items: list[pytest.Item]) -> None:
    """Skip `db` tests when PostgreSQL is unreachable — unless CI forbids it.

    A skipped test reports success, so a skip that becomes permanent is
    indistinguishable from coverage that was never written. `REQUIRE_DB=1`,
    set by Backend CI, converts the skip into an error: locally the suite stays
    runnable without Docker, and in CI the database coverage cannot vanish
    quietly.
    """
    db_items = [item for item in items if item.get_closest_marker("db")]
    if not db_items or _database_is_reachable():
        return

    dsn_host = os.environ["DATABASE_URL"].rsplit("@", 1)[-1]
    if os.environ.get("REQUIRE_DB") == "1":
        raise pytest.UsageError(
            f"REQUIRE_DB=1 but PostgreSQL at {dsn_host} is unreachable, so "
            f"{len(db_items)} database test(s) would be skipped. In CI this is "
            f"a failure, not a skip."
        )
    marker = pytest.mark.skip(
        reason=(
            f"PostgreSQL at {dsn_host} is unreachable. Start it with "
            f"`docker compose up -d postgres`."
        )
    )
    for item in db_items:
        item.add_marker(marker)


@pytest.fixture
async def db_session() -> AsyncIterator[Any]:
    """A session bound to the real test database, rolled back afterwards.

    Each test runs inside a transaction that is rolled back on teardown, so
    tests never observe each other's writes and the suite needs no truncation
    between them. Pair it with ``migrated_schema`` when the test needs tables.
    """
    from backend.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.begin()
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def engine_isolation() -> AsyncIterator[None]:
    """Give each test its own engine, and dispose it afterwards.

    `get_engine` is cached for the life of the process, which is correct in
    production: one event loop, one connection pool. pytest-asyncio gives every
    test its own event loop, so a connection pooled by one test belongs to a
    loop that is closed by the time the next test checks it out. With
    `pool_pre_ping=True` the symptom is a `RuntimeError: Event loop is closed`
    raised from inside the ping, which points at SQLAlchemy's pool rather than
    at the cause.

    Disposing between tests is the accommodation the test harness needs; the
    alternative — swapping in NullPool for tests — would mean the pooling
    configuration that actually ships is never exercised. It also means
    `dispose_engine` itself runs on every database test.

    Opt in per module:

        pytestmark = [pytest.mark.db, pytest.mark.usefixtures("engine_isolation")]
    """
    from backend.db.engine import dispose_engine, get_engine
    from backend.db.session import get_sessionmaker

    # The sessionmaker caches the engine it is bound to, so clearing one
    # without the other would hand out sessions on a disposed engine.
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        yield
    finally:
        await dispose_engine()
        get_sessionmaker.cache_clear()


@pytest.fixture(scope="session")
def migrated_schema() -> None:
    """Bring the test database up to head, once per session.

    Uses `alembic upgrade head` rather than `Base.metadata.create_all()`. They
    are not equivalent: create_all builds the schema the models describe, which
    is exactly the schema the migration is supposed to produce and therefore
    proves nothing about whether it does. Running the migration means the thing
    under test is the thing that will run in production.

    Opt in per module alongside the database marker:

        pytestmark = [
            pytest.mark.db,
            pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
        ]
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr}")


@pytest.fixture
async def committing_session() -> AsyncIterator[Any]:
    """A session whose commits are real, with the tables truncated afterwards.

    ``db_session`` wraps each test in a transaction and rolls it back, which is
    the right default — but it cannot be used to observe a commit. A service
    that commits inside that outer transaction ends it, the teardown rollback
    becomes a no-op, and the rows leak into the next test.

    So transaction tests get a plain session and clean up by truncating. Order
    matters in the TRUNCATE only for readability; CASCADE handles the foreign
    keys either way.
    """
    from sqlalchemy import text

    from backend.db.engine import get_engine
    from backend.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        try:
            yield session
        finally:
            await session.rollback()
            async with get_engine().begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE document_chunks, documents, users "
                        "RESTART IDENTITY CASCADE"
                    )
                )
