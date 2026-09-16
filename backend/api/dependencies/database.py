"""Database connection dependencies."""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_sessionmaker
from vector_db.qdrant.client import build_qdrant_client
from vector_db.qdrant.config import QdrantConfig
from memory_system.redis.client import get_redis_client


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped PostgreSQL session.

    Guarantees rollback on failure and close always. It deliberately does
    **not** commit.

    Two reasons, both specific to this project:

    * ADR-0003 fixes a deletion order that spans two stores — "soft-delete in
      PostgreSQL (committed first) -> delete vectors in Qdrant -> finalise". The
      service has to choose *when* the PostgreSQL half commits, so the commit
      cannot belong to a dependency that only runs at teardown.
    * Since FastAPI 0.106 the code after ``yield`` runs once the response is
      already being sent, and exceptions raised there no longer reach exception
      handlers. A commit is the statement most likely to fail — on a constraint
      violation or a lost connection — and failing there would produce an
      unhandleable error after a 200 had been promised.

    So: repositories flush, services commit, this dependency cleans up. Sprint
    M1/S1.3 adds the repositories; M1/S1.4 the first service to commit.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_vector_db():
    """FastAPI dependency for a Qdrant client.

    Builds one per request and closes it, because M3/S3.5 removed the cached
    factory this used to call: an `@lru_cache`d client froze configuration at
    first import and shared one event loop's connection pool with every caller.

    TODO(M4): retrieval should take a client owned by the application's
    lifespan, as the worker does, rather than one per request.
    """
    from backend.config.settings import get_settings

    client = build_qdrant_client(QdrantConfig.from_settings(get_settings()))
    try:
        yield client
    finally:
        await client.close()


async def get_memory_store():
    """FastAPI dependency for Redis client."""
    yield get_redis_client()
