"""Document-related models."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class DocumentType(str, Enum):
    PDF = "pdf"
    TEXT = "text"
    NOTE = "note"
    REPORT = "report"


class DocumentStatus(str, Enum):
    """Where a document is in ingestion — ADR-0009 §4, with one stage added.

    PENDING → PROCESSING → PARSED → … → READY
                  ↘ FAILED (from any PROCESSING)

    * PENDING    accepted and stored; no stage has run to completion yet, and
                 none is running now
    * PROCESSING a worker holds the claim and is working on it now
    * PARSED     its text has been extracted and stored, page by page; it is not
                 yet chunked, embedded or searchable (M3/S3.3)
    * READY      processed and available downstream — per ADR-0009, the M3
                 definition of done ("poll until ready, then search") and the
                 project vision, chunked, embedded and searchable. Nothing
                 produces it yet.
    * FAILED     permanently unprocessable; `failure_reason` says why

    PARSED exists because READY must keep meaning "searchable": a client that
    polls for READY and then searches cannot be told READY for a document with
    no chunks. Stages after PARSED are later sprints'.

    FAILED replaced the scaffold's ERROR in M3/S3.2 (migration 0004). ADR-0009,
    the accepted decision, names it FAILED; nothing had ever written ERROR.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    PARSED = "parsed"
    READY = "ready"
    FAILED = "failed"


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
    # Facts about the stored bytes (ADR-0008), set by upload. None for documents
    # created before upload existed. The storage key is deliberately not here:
    # it is an internal location, and this model is what reads return.
    content_hash: Optional[str] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    page_count: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UploadOutcome(BaseModel):
    """What an upload produced: the document, and whether this upload created it.

    `created` is False when the caller already had a live document with the same
    content (ADR-0010). The distinction reaches the client as 202 versus 200.
    """

    model_config = ConfigDict(frozen=True)

    document: Document
    created: bool


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
