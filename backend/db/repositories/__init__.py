"""Repositories — the data-access boundary.

Three explicit repositories, none of them inheriting a generic CRUD base. The
reason is in `shared/interfaces/repository.py`: `BaseRepository` exposes
`get_by_id`, `get_all`, `update` and `delete` with no owner, and
`docs/security/principles.md` requires the ownership filter to be a *required
parameter of the repository signature* — "the safe path must be the only path".
Those cannot both hold for an owned resource.

`UserRepository` may take ids directly because a user is the ownership root.
`DocumentRepository` and `DocumentChunkRepository` require `user_id` on every
method that can reach a row, and put it in the SQL.

Repositories flush; they never commit. The transaction belongs to the caller,
because ADR-0003 fixes a deletion order that spans PostgreSQL and Qdrant and
the service has to choose when the PostgreSQL half lands.
"""

from backend.db.repositories.document import DocumentRepository
from backend.db.repositories.document_chunk import DocumentChunkRepository
from backend.db.repositories.user import UserRepository

__all__ = ["DocumentChunkRepository", "DocumentRepository", "UserRepository"]
