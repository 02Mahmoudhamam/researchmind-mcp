"""Declarative base for every ORM model.

This module intentionally contains no models. It exists so that
``Base.metadata`` — and in particular its naming convention — is established
once, before any table is declared or any migration is generated.
"""

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

# Deterministic names for every index and constraint.
#
# This must be in place BEFORE the first `alembic revision --autogenerate`, and
# it is effectively now-or-never. Without it PostgreSQL assigns its own names
# (`documents_user_id_fkey`, `documents_pkey`, …) which SQLAlchemy does not know
# and therefore cannot reference. A generated `downgrade()` then has no name to
# drop a constraint by, so migrations stop being reversible — and fixing it
# afterwards means renaming constraints on a populated schema.
#
# `column_0_N_name` rather than `column_0_label` for ix/uq: the label form names
# only the first column, so two multi-column indexes beginning with the same
# column would collide. `documents` is planned to carry both
# (user_id, created_at) and (user_id, content_hash) — with the label form both
# would be called `ix_documents_user_id`.
#
# PostgreSQL truncates identifiers at 63 characters; the longest name these
# templates can currently produce is well inside that.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all ORM models.

    ``AsyncAttrs`` supplies ``awaitable_attrs``, so an unloaded relationship can
    be awaited explicitly. Without it, touching one under asyncio raises
    ``MissingGreenlet`` — lazy loading is I/O, and there is no await point to
    hang it on. Repositories should still prefer eager loading (``selectinload``)
    for anything they know they need; this is the escape hatch, not the plan.

    Note for anyone adding a model: ``metadata`` is a reserved attribute on a
    declarative class — it is the :class:`~sqlalchemy.MetaData` object below.
    Three of the project's Pydantic models have a field of that name
    (``Document.metadata``, ``DocumentChunk.metadata``, ``AgentInput.metadata``),
    so the corresponding columns must be mapped under a different attribute
    name, e.g. ``doc_metadata``. The Pydantic field name — and therefore the
    API contract — does not change.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
