"""The unit of work that hands an uploaded document to ingestion.

ADR-0009 moves ingestion out of the request into a worker. A worker has no
request: no Authorization header, no token, no `Principal`. So everything it
needs to process a document **correctly and for the right owner** is decided
here, by the request that authenticated the upload, and carried with the job.

In particular the worker never has to work out who owns a document. `owner_id`
is `Principal.user_id` at the moment the upload was accepted; the worker must
not replace it with anything it looks up, and must not trust anything else.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict

from shared.models.document import DocumentStatus


class IngestionJob(BaseModel):
    """An immutable instruction to ingest one stored document.

    Plain values only, so it serialises into any queue without dragging an
    object graph behind it. Deliberately absent: the `Principal` (an
    authenticated identity is a request-time fact, not something to replay
    later), any credential, and the client's filename (display metadata that
    has no bearing on processing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str

    # Assigned by the application from `Principal.user_id`. Never from the
    # client and never re-derived by the worker.
    owner_id: str

    # Where the bytes are, as `document_storage_key` built it.
    storage_key: str

    # Lets the worker verify the bytes it reads are the bytes that were
    # validated: a mismatch is corruption or tampering, and a terminal failure.
    content_hash: str

    # Sniffed from the content, never the client's Content-Type. Selects the
    # parser when there is more than one.
    mime_type: str


class IngestionReason(str, Enum):
    """Why a job was rejected or a document failed. Stable codes, not prose.

    Safe to store and to show an operator: none of them carries a path, an
    exception message, a filename or content. The *outcome* says whether the
    reason was persisted on the document (`failed`) or only reported for the job
    (`rejected`) — the same code can mean either, and the difference matters.
    """

    # The job itself cannot be trusted. The document, if any, is left untouched.
    INVALID_JOB = "invalid_job"
    DOCUMENT_NOT_FOUND = "document_not_found"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    STORAGE_KEY_MISMATCH = "storage_key_mismatch"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    MIME_TYPE_MISMATCH = "mime_type_mismatch"

    # The document cannot be processed. Persisted as `failure_reason`.
    STORAGE_MISSING = "storage_missing"
    STORAGE_READ_FAILURE = "storage_read_failure"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    PROCESSING_FAILURE = "processing_failure"
    STALE_PROCESSING = "stale_processing"

    # Documents that were `error` before migration 0004 introduced reasons.
    UNKNOWN = "unknown"


class IngestionOutcome(str, Enum):
    """What one delivery of a job did. A job can be delivered more than once."""

    # Checked and found sound; the claim is released and the document is
    # `pending` again, because no processing stage exists yet to take it
    # further (M3/S3.2). Nothing is claimed to have been parsed.
    VERIFIED = "verified"
    # Another delivery holds the claim. Nothing was done.
    SKIPPED_IN_PROGRESS = "skipped_in_progress"
    # The document is already `ready` or `failed`. Nothing was done.
    SKIPPED_TERMINAL = "skipped_terminal"
    # The job was not acted on; the document was not changed.
    REJECTED = "rejected"
    # The document is now `failed`, with the reason persisted.
    FAILED = "failed"
    # The claim was taken away mid-run — by the reaper — and this delivery did
    # not overwrite what replaced it.
    CLAIM_LOST = "claim_lost"


class IngestionResult(BaseModel):
    """The deterministic result of one delivery."""

    model_config = ConfigDict(frozen=True)

    outcome: IngestionOutcome
    document_id: str | None = None
    reason: IngestionReason | None = None


class IngestionTarget(BaseModel):
    """What the worker needs to know about a document, read owner-scoped.

    A dedicated type, like `UserCredentials`: the storage key is internal and
    stays off the `Document` model that reads return to clients.
    """

    model_config = ConfigDict(frozen=True)

    document_id: str
    owner_id: str
    status: DocumentStatus
    storage_key: str | None
    content_hash: str | None
    mime_type: str | None


class RecoveryResult(BaseModel):
    """What one pass of the recovery sweep did (M3/S3.3)."""

    model_config = ConfigDict(frozen=True)

    # Documents this pass queued a job for.
    requeued: tuple[str, ...] = ()
    # Documents that already had a job queued, deferred or running.
    already_queued: int = 0
    # Rows whose stored fields cannot form a valid job. Logged, left untouched.
    skipped: int = 0
