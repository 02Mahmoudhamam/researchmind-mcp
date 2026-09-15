"""ARQ worker configuration — the entrypoint `arq` is pointed at.

    arq backend.ingestion.worker_settings.WorkerSettings

ARQ reads these as class attributes, so configuration is read when this module
is imported. That is correct for a worker process — its settings cannot change
without a restart — and it is why this lives apart from the task functions,
which tests import freely.
"""

from arq.worker import func

from backend.config.settings import get_settings
from backend.ingestion.queue import INGEST_DOCUMENT_TASK, redis_settings_from
from backend.ingestion.worker import ingest_document, shutdown, startup

_settings = get_settings()


class WorkerSettings:
    functions = [
        func(
            ingest_document,
            name=INGEST_DOCUMENT_TASK,
            max_tries=_settings.INGEST_MAX_TRIES,
            timeout=_settings.INGEST_JOB_TIMEOUT_SECONDS,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    # The worker, unlike the API, waits for Redis: retrying is its job.
    redis_settings = redis_settings_from(_settings, conn_retries=5)
    max_jobs = _settings.ARQ_MAX_JOBS
    job_timeout = _settings.INGEST_JOB_TIMEOUT_SECONDS
    max_tries = _settings.INGEST_MAX_TRIES
    # Results are not kept. The document's status is the record of what
    # happened, and a kept result would block re-enqueueing that document's job
    # id for as long as it was kept.
    keep_result = 0
