"""The ingestion queue as it stands in M3/S3.1: accepted, not yet consumed.

ADR-0009 runs ingestion in an ARQ worker over Redis. That worker, its queue and
its compose service are the next sprint's. Until then uploads still produce a
complete, correct `IngestionJob` — owner, storage key, content hash — and hand
it to this queue, which records that it was received and does nothing else.

This is stated rather than disguised. A document uploaded now is `pending`, the
API says `pending`, and it stays `pending` until a worker exists to change it.
Nothing here reports work that did not happen.
"""

from shared.models.ingestion import IngestionJob
from shared.utils.logger import get_logger

_log = get_logger(__name__)


class DeferredIngestionQueue:
    """`IngestionQueue` with no consumer yet. Replaced by the ARQ queue in M3/S3.2."""

    async def enqueue(self, job: IngestionJob) -> None:
        # Identifiers only. No filename and no content: neither is in the job,
        # and neither belongs in a log line (principles.md §7).
        _log.info(
            "ingestion.job.deferred",
            document_id=job.document_id,
            owner_id=job.owner_id,
            content_hash=job.content_hash,
            reason="no ingestion worker until M3/S3.2",
        )
