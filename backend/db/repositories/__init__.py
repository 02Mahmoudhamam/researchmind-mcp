"""Repositories — the data-access boundary.

Explicit repositories, none inheriting a generic CRUD base. The reason is in
``shared/interfaces/repository.py``.

Repositories flush; they never commit. The transaction belongs to the caller,
because ADR-0003 fixes a deletion order that spans PostgreSQL and Qdrant and
the service has to choose when the PostgreSQL half lands.
"""

from backend.db.repositories.document import DocumentRepository
from backend.db.repositories.user import UserRepository

__all__ = ["DocumentRepository", "UserRepository"]
