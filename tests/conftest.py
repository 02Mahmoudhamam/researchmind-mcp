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

import dataclasses
import os
from typing import Any, AsyncIterator

_TEST_ENV: dict[str, str] = {
    # Required by Settings and has no default; without it nothing imports.
    "ANTHROPIC_API_KEY": "test-anthropic-api-key",
    # Placeholders. Sprint M2/S2.1 makes the application refuse to start on the
    # "changeme" defaults outside development, so tests set explicit values.
    # At least 32 characters. PyJWT 2.13 raises InsecureKeyLengthWarning below
    # that (RFC 7518 §3.2), and Settings enforces the same floor outside
    # development — a test key shorter than a real one would be testing a
    # configuration nothing may ship.
    #
    # Deliberately repetitive, so the gitleaks scan in CI reads them as the
    # fixtures they are rather than as leaked credentials.
    "SECRET_KEY": "test-secret-test-secret-test-secret-test",
    "JWT_SECRET": "test-jwt-test-jwt-test-jwt-test-jwt-test",
    "APP_ENV": "test",
    "DEBUG": "false",
    "LOG_LEVEL": "WARNING",
    # Redis database 15, not 0. The ARQ worker tests flush their database before
    # and after each test; on a developer's machine database 0 is the one
    # docker compose's Redis is actually used with.
    "REDIS_DB": "15",
    # A collection name no deployment uses. Each test still builds its own
    # uniquely-suffixed collection and drops it, but this is the net
    # underneath: a test that forgot would not touch `researchmind`.
    "QDRANT_COLLECTION": "researchmind_test",
}

for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value

# Document storage goes to a directory created for this run, never the default.
#
# STORAGE_ROOT defaults to `uploads`, relative to the working directory — which
# for a test run is the repository checkout. A test that forgot to override it
# would write blobs into the developer's real storage directory, where the
# gitignore hides them. Setting it here, before anything imports Settings, means
# forgetting is harmless. Tests that inspect storage still use their own
# `tmp_path`; this is the net underneath them.
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_SESSION_STORAGE_ROOT = tempfile.mkdtemp(prefix="researchmind-test-storage-")
os.environ["STORAGE_ROOT"] = _SESSION_STORAGE_ROOT
atexit.register(shutil.rmtree, _SESSION_STORAGE_ROOT, True)

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


def _redis_is_reachable() -> bool:
    """Open a real TCP connection to the configured Redis, once per run."""
    import socket

    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    try:
        with socket.create_connection((host, port), 2.0):
            return True
    except OSError:
        return False


_EMBEDDING_PROVIDER: object | None = None


def _embedding_model_is_available() -> bool:
    """Load the embedding model once per run, and keep it for the tests.

    The parallel to `_database_is_reachable` is exact: this is the real thing,
    not a stand-in. A cold cache downloads ~67 MB, which is why CI pre-fetches
    it and the image bakes it in; without it, the tests that need it skip.
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is not None:
        return True
    try:
        from document_processing.embedder import FastEmbedProvider

        _EMBEDDING_PROVIDER = FastEmbedProvider(
            os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            cache_dir=os.environ.get("EMBEDDING_CACHE_DIR") or None,
        )
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def embedding_provider() -> Any:
    """The one loaded model, shared by every test that needs it."""
    assert _EMBEDDING_PROVIDER is not None, "the collection gate should have loaded it"
    return _EMBEDDING_PROVIDER


def _qdrant_is_reachable() -> bool:
    """Open a real TCP connection to the configured Qdrant, once per run."""
    import socket

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    try:
        with socket.create_connection((host, port), 2.0):
            return True
    except OSError:
        return False


@pytest.fixture()
async def vector_store(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """A `QdrantVectorStore` over a collection built for this test alone.

    Named after the test and dropped afterwards, so tests cannot see each
    other's vectors and a failure cannot strand points that make the next run
    pass — or fail — for the wrong reason.
    """
    import re
    import uuid

    from backend.config.settings import get_settings
    from vector_db.qdrant.client import build_qdrant_client
    from vector_db.qdrant.config import QdrantConfig
    from vector_db.qdrant.repository import QdrantVectorStore

    safe = re.sub(r"[^A-Za-z0-9_]", "_", request.node.name)[:60]
    config = QdrantConfig.from_settings(get_settings())
    config = dataclasses.replace(
        config, collection_name=f"test_{safe}_{uuid.uuid4().hex[:8]}"
    )
    client = build_qdrant_client(config)
    try:
        yield QdrantVectorStore(client, config)
    finally:
        try:
            await client.delete_collection(config.collection_name)
        finally:
            await client.close()


def _require_or_skip(
    items: list[pytest.Item],
    *,
    reachable: bool,
    required_by: str,
    what: str,
    how_to_start: str,
) -> None:
    """Skip `items` when their service is unreachable — or fail, if CI requires it.

    A skipped test reports success, so a skip that becomes permanent is
    indistinguishable from coverage that was never written. `REQUIRE_DB=1` and
    `REQUIRE_REDIS=1`, set by Backend CI, turn the skip into an error: locally
    the suite stays runnable without Docker, and in CI the coverage cannot
    vanish quietly.
    """
    if not items or reachable:
        return
    if os.environ.get(required_by) == "1":
        raise pytest.UsageError(
            f"{required_by}=1 but {what} is unreachable, so {len(items)} test(s) "
            f"would be skipped. In CI this is a failure, not a skip."
        )
    marker = pytest.mark.skip(
        reason=f"{what} is unreachable. Start it with `{how_to_start}`."
    )
    for item in items:
        item.add_marker(marker)


def pytest_collection_modifyitems(config: object, items: list[pytest.Item]) -> None:
    """Gate each marker on the service or model its tests genuinely need."""
    db_items = [item for item in items if item.get_closest_marker("db")]
    if db_items:
        dsn_host = os.environ["DATABASE_URL"].rsplit("@", 1)[-1]
        _require_or_skip(
            db_items,
            reachable=_database_is_reachable(),
            required_by="REQUIRE_DB",
            what=f"PostgreSQL at {dsn_host}",
            how_to_start="docker compose up -d postgres",
        )

    redis_items = [item for item in items if item.get_closest_marker("redis")]
    if redis_items:
        _require_or_skip(
            redis_items,
            reachable=_redis_is_reachable(),
            required_by="REQUIRE_REDIS",
            what="Redis",
            how_to_start="docker compose up -d redis",
        )

    qdrant_items = [item for item in items if item.get_closest_marker("qdrant")]
    if qdrant_items:
        _require_or_skip(
            qdrant_items,
            reachable=_qdrant_is_reachable(),
            required_by="REQUIRE_QDRANT",
            what="Qdrant",
            how_to_start="docker compose up -d qdrant",
        )

    model_items = [item for item in items if item.get_closest_marker("embeddings")]
    if model_items:
        _require_or_skip(
            model_items,
            reachable=_embedding_model_is_available(),
            required_by="REQUIRE_EMBEDDINGS",
            what="the embedding model",
            how_to_start="poetry run python scripts/fetch-embedding-model.py",
        )


class _AcceptingIngestionQueue:
    """An `IngestionQueue` that accepts every job and keeps it in memory."""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> bool:
        self.jobs.append(job)
        return True


@pytest.fixture(autouse=True)
def _ingestion_queue_without_redis(request: pytest.FixtureRequest) -> Any:
    """Stand in for Redis wherever a test is not about Redis.

    Since M3/S3.2 the API enqueues ingestion jobs to Redis. Most tests that
    upload — authorisation matrices, storage, validation — are about something
    else, and should not need a Redis server to run or fail when one is absent.
    They get a queue that accepts jobs in memory.

    Tests marked `redis` opt out and use the real provider against a real Redis;
    that is where the ARQ queue and worker are tested. Tests that assert on the
    jobs themselves install their own recorder, which replaces this one.
    """
    if request.node.get_closest_marker("redis"):
        yield
        return

    from backend.api.app import app
    from backend.api.dependencies.services import get_ingestion_queue

    app.dependency_overrides[get_ingestion_queue] = _AcceptingIngestionQueue
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_ingestion_queue, None)


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
