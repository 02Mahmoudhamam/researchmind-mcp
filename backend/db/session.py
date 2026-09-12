"""Session factory.

One :class:`~sqlalchemy.ext.asyncio.AsyncSession` per request, created by the
``get_db_session`` dependency in ``backend/api/dependencies/database.py``.
"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.db.engine import get_engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first call."""
    return async_sessionmaker(
        bind=get_engine(),
        # Without this, SQLAlchemy expires every attribute on commit and
        # reloads it on next access. Under asyncio that lazy reload is I/O
        # outside an await, which raises MissingGreenlet — an error that names
        # neither the attribute nor the commit that caused it. Since the
        # repository layer maps ORM rows to Pydantic models after committing,
        # every such read would trip it.
        expire_on_commit=False,
    )
