"""Database infrastructure: declarative base, engine and session factory.

Contains no ORM models and no schema. Models and migrations arrive in Sprint
M1/S1.2; repositories in M1/S1.3.
"""

from backend.db.base import NAMING_CONVENTION, Base
from backend.db.engine import dispose_engine, get_engine
from backend.db.session import get_sessionmaker

__all__ = [
    "Base",
    "NAMING_CONVENTION",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
]
