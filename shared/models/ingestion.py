"""The unit of work that hands an uploaded document to ingestion.

ADR-0009 moves ingestion out of the request into a worker. A worker has no
request: no Authorization header, no token, no `Principal`. So everything it
needs to process a document **correctly and for the right owner** is decided
here, by the request that authenticated the upload, and carried with the job.

In particular the worker never has to work out who owns a document. `owner_id`
is `Principal.user_id` at the moment the upload was accepted; the worker must
not replace it with anything it looks up, and must not trust anything else.
"""

from pydantic import BaseModel, ConfigDict


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
