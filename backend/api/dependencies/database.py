"""Database connection dependencies."""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_sessionmaker
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


# `get_vector_db` was removed in M4/S4.2. It built a Qdrant client per request
# and carried a `TODO(M4)` saying retrieval should take one owned by the
# application's lifespan. That is now what happens — `backend/api/composition.py`
# builds one per process — and it had no callers, so nothing replaced it.


async def get_memory_store():
    """FastAPI dependency for Redis client."""
    yield get_redis_client()
