"""Document request/response schemas."""
from pydantic import BaseModel
from typing import List, Optional
from shared.models.document import Document, DocumentStatus


class DocumentResponse(BaseModel):
    id: str
    filename: str
    status: DocumentStatus
    metadata: dict = {}


class DocumentListResponse(BaseModel):
    documents: List[DocumentResponse]
    total: int
