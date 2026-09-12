"""Document-related models."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class DocumentType(str, Enum):
    PDF = "pdf"
    TEXT = "text"
    NOTE = "note"
    REPORT = "report"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class DocumentMetadata(BaseModel):
    title: Optional[str] = None
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    journal: Optional[str] = None
    doi: Optional[str] = None
    abstract: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    id: str
    user_id: str
    filename: str
    doc_type: DocumentType
    status: DocumentStatus = DocumentStatus.PENDING
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    chunk_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DocumentChunk(BaseModel):
    id: str
    document_id: str
    content: str
    embedding: Optional[List[float]] = None
    chunk_index: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    chunk: DocumentChunk
    score: float
    document: Optional[Document] = None
