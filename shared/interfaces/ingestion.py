"""Where accepted uploads are handed off for ingestion (ADR-0009)."""

from typing import Protocol

from shared.models.ingestion import IngestionJob


class IngestionQueue(Protocol):
    """Accepts ingestion jobs. What consumes them is not the uploader's business.

    `enqueue` is called only after the document row has been **committed**, so a
    worker can never be handed a job for a document that does not exist.
    """

    async def enqueue(self, job: IngestionJob) -> bool:
        """Hand the job off. Raises if it could not be accepted.

        Returns True if this call queued the job, False if a job for the same
        document was already queued or running — which is not a failure: the
        document will be ingested either way, once (M3/S3.3).
        """
        ...
