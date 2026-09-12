"""SQLAlchemy persistence models.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic's autogenerate reads. A model that is not imported here is
invisible to migrations.

These are storage models. The API and MCP contracts are the Pydantic models in
``shared/models/``; mapping between the two belongs to the repositories in
Sprint M1/S1.3.
"""

from backend.db.models.document import DocumentORM
from backend.db.models.document_chunk import DocumentChunkORM
from backend.db.models.user import UserORM

__all__ = ["DocumentChunkORM", "DocumentORM", "UserORM"]
