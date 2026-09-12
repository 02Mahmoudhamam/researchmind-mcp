"""Alembic environment.

Async, because the application is (ADR-0003) and a second synchronous driver
would be a second thing to install, configure and keep in step.

The database URL comes from ``backend.config.settings``, never from
``alembic.ini``. One source of configuration means migrations cannot be applied
to a different database than the one the application talks to.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.config.settings import get_settings
from backend.db.base import Base

# Importing the models package is what registers the tables on Base.metadata.
# Without it autogenerate sees an empty schema and cheerfully proposes dropping
# every table in the database.
import backend.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    """Shared options for both offline and online runs."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Neither is on by default, and without them autogenerate silently
        # ignores a column whose type or default changed — producing an empty
        # migration that looks like "no drift" when the schema has in fact moved.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful for review and for handing a change to a DBA; `alembic upgrade head
    --sql` goes through here.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations through an async connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # Migrations are a short-lived process. Pooling would keep connections
        # open past the work and delay exit.
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
