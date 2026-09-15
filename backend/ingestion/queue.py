"""The ingestion queue: ARQ over Redis (ADR-0009).

Replaces M3/S3.1's `DeferredIngestionQueue`, which accepted jobs and did nothing
with them. The contract is unchanged — `IngestionQueue.enqueue(job)`, called only
after the document row is committed — so the upload service did not change to
use it; only the dependency provider did.

What the API knows about ARQ is confined to this module: a task name, a job id
and a connection. The worker that consumes the jobs is `backend/ingestion/worker.py`.

Two implementations of one contract, differing only in who owns the connection:
`ArqIngestionQueue` opens one per enqueue (the API), `PooledArqIngestionQueue`
uses a connection it is handed (the worker's recovery sweep, M3/S3.3).
"""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.config.settings import Settings
from shared.models.ingestion import IngestionJob
from shared.utils.logger import get_logger

_log = get_logger(__name__)

# The name the worker registers `ingest_document` under. One constant, imported
# by both sides, so the producer and the consumer cannot disagree.
INGEST_DOCUMENT_TASK = "ingest_document"


def ingestion_job_id(document_id: str) -> str:
    """ARQ job id for a document: one live job per document.

    ARQ refuses to enqueue a job whose id is already queued or running, so a
    second enqueue for the same document is a no-op rather than a second
    worker racing the first. The database claim is still the real guard; this
    keeps redundant work out of the queue in the first place.
    """
    return f"ingest-document:{document_id}"


def redis_settings_from(settings: Settings, *, conn_retries: int = 0) -> RedisSettings:
    """ARQ connection settings from application settings.

    `conn_retries=0` for the API: ARQ's default retries a refused connection
    five times a second apart, which would hold an upload request open for five
    seconds before admitting Redis is down. One attempt, then an honest failure.
    """
    return RedisSettings(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        database=settings.REDIS_DB,
        conn_timeout=1,
        conn_retries=conn_retries,
    )


async def enqueue_ingestion_job(redis: ArqRedis, job: IngestionJob) -> bool:
    """Queue `job` on an open ARQ connection. True only if this call queued it.

    False means a job for this document already exists — queued, deferred for a
    retry, or running. ARQ checks for the job id and writes the job inside one
    WATCH/MULTI transaction, so of any number of concurrent callers exactly one
    queues it; the rest get False. That is the whole of the duplicate-job guard,
    and it is ARQ's, not a second one built beside it.

    The payload is the job as JSON-compatible values, not a pickled model: the
    worker validates it back into an `IngestionJob`, so a job written by one
    version of the code and read by another fails validation loudly instead of
    unpickling into something subtly different.
    """
    queued = await redis.enqueue_job(
        INGEST_DOCUMENT_TASK,
        job.model_dump(mode="json"),
        _job_id=ingestion_job_id(job.document_id),
    )
    return queued is not None


class ArqIngestionQueue:
    """`IngestionQueue` backed by ARQ.

    A connection per enqueue rather than a pool held for the life of the
    process. Uploads are infrequent and already slow — a PDF is read, hashed and
    written first — so the connection costs nothing that matters, and nothing is
    left bound to an event loop or needing to be closed at shutdown. Pooling is
    a hardening decision for when upload volume makes it one.
    """

    def __init__(self, redis_settings: RedisSettings) -> None:
        self._redis_settings = redis_settings

    async def enqueue(self, job: IngestionJob) -> bool:
        """Queue the job. Raises if Redis cannot accept it."""
        redis = await create_pool(self._redis_settings)
        try:
            queued = await enqueue_ingestion_job(redis, job)
        finally:
            await redis.aclose()
        _log.info(
            "ingestion.job.enqueued" if queued else "ingestion.job.already_queued",
            document_id=job.document_id,
            owner_id=job.owner_id,
            content_hash=job.content_hash,
        )
        return queued


class PooledArqIngestionQueue:
    """`IngestionQueue` over a connection it does not own: the worker's.

    ARQ hands every task and cron job the worker's own pool as `ctx["redis"]`.
    The recovery sweep enqueues through that rather than opening a connection
    per document. Closing it is the worker's business, not this class's.
    """

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def enqueue(self, job: IngestionJob) -> bool:
        """Queue the job. Raises if Redis cannot accept it."""
        return await enqueue_ingestion_job(self._redis, job)
