"""Async engine construction and teardown.

The engine is built lazily and cached. It is never constructed at import time:
``vector_db/qdrant/config.py`` and ``memory_system/redis/config.py`` both call
``get_settings()`` at module scope and bind the result as class attributes,
which freezes configuration at import and defeats per-test overrides. Repeating
that here would be worse, because an engine also allocates a connection pool —
importing this module would then require a reachable database, and test
collection would fail on any machine without one.

``create_async_engine`` itself does not connect; the pool is populated on first
use. Nothing in this module performs I/O.
"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.config.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call."""
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        # Validates a pooled connection before handing it out. Without this, a
        # PostgreSQL restart leaves the pool holding dead sockets and the next
        # request fails rather than reconnecting.
        pool_pre_ping=True,
    )


async def dispose_engine() -> None:
    """Close the connection pool, if one was ever created.

    Checks the cache rather than calling ``get_engine()``, which would build an
    engine purely in order to dispose of it — and would connect to nothing on a
    process that never touched the database.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
        get_engine.cache_clear()
