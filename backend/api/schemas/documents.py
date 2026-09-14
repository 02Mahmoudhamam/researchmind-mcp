"""Document request/response schemas."""

from typing import Any, List

from pydantic import BaseModel, Field

from shared.models.document import DocumentStatus


class DocumentResponse(BaseModel):
    """What a client sees of a document. Unchanged in shape by M3/S3.1.

    Deliberately without the storage key, content hash, owner id or sizes the
    database now holds: where bytes live is internal (ADR-0008), and widening
    the response is a contract change with no consumer yet asking for it.
    """

    id: str
    filename: str
    status: DocumentStatus
    # `dict[str, Any]` with a factory: the same JSON as the bare `dict = {}` it
    # replaces, now type-checkable under the strict gate.
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentListResponse(BaseModel):
    documents: List[DocumentResponse]
    total: int
