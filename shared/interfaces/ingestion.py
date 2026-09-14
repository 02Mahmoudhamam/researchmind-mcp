"""Where accepted uploads are handed off for ingestion (ADR-0009)."""

from typing import Protocol

from shared.models.ingestion import IngestionJob


class IngestionQueue(Protocol):
    """Accepts ingestion jobs. What consumes them is not the uploader's business.

    `enqueue` is called only after the document row has been **committed**, so a
    worker can never be handed a job for a document that does not exist.
    """

    async def enqueue(self, job: IngestionJob) -> None:
        """Hand the job off. Raises if it could not be accepted."""
        ...
