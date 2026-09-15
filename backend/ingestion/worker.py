"""The ARQ worker's task functions — the process ADR-0009 moves ingestion into.

Thin by design. Each function turns an ARQ call into a call on
`IngestionService` and turns the service's answer back into ARQ's vocabulary:
a return value, or `Retry`. What a job is *allowed* to do is decided in the
service, which does not import ARQ.

Run with:

    arq backend.ingestion.worker_settings.WorkerSettings

The settings class lives in its own module because ARQ reads it as class
attributes, which means reading configuration at import. Keeping it out of this
module lets tests import the task functions without freezing settings.
"""

from typing import Any

from arq import Retry
from pydantic import ValidationError

from backend.config.settings import get_settings
from backend.db.engine import dispose_engine
from backend.db.session import get_sessionmaker
from backend.services.ingestion_service import IngestionService, TransientIngestionError
from backend.storage import LocalStorage
from shared.interfaces.storage import Storage
from shared.models.ingestion import (
    IngestionJob,
    IngestionOutcome,
    IngestionReason,
    IngestionResult,
)
from shared.utils.logger import get_logger

_log = get_logger(__name__)

# Delay before each retry of a transient failure, by attempt. Backoff, capped:
# the first retry comes quickly in case the fault was momentary; later ones
# give storage or the database room to recover.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (2, 10, 30, 60)


def retry_delay_seconds(job_try: int) -> int:
    """How long to wait before the retry that follows attempt `job_try`."""
    return RETRY_DELAYS_SECONDS[min(max(job_try, 1), len(RETRY_DELAYS_SECONDS)) - 1]


async def startup(ctx: dict[str, Any]) -> None:
    """Build the worker's collaborators once per process.

    The composition root for the worker, as the dependency providers are for
    the API: the only place a concrete storage backend is named. Everything
    below receives `Storage`.
    """
    settings = get_settings()
    storage: Storage = LocalStorage(settings.STORAGE_ROOT)
    ctx["storage"] = storage
    ctx["ingest_max_tries"] = settings.INGEST_MAX_TRIES
    ctx["stale_processing_seconds"] = settings.INGEST_STALE_PROCESSING_SECONDS
    _log.info("ingestion.worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    _log.info("ingestion.worker.stopped")


async def ingest_document(ctx: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Run one delivery of an ingestion job.

    Returns the result as plain values. Raises `Retry` for a transient failure
    that has attempts left; on the final attempt the service records a
    terminal failure instead, so a job never retries forever (ADR-0009 §5).

    A payload that is not a valid `IngestionJob` is rejected without retry and
    without touching any document — a malformed job cannot be trusted to name
    the right one.
    """
    try:
        job = IngestionJob.model_validate(payload)
    except ValidationError:
        _log.error("ingestion.rejected", reason=IngestionReason.INVALID_JOB.value)
        return IngestionResult(
            outcome=IngestionOutcome.REJECTED, reason=IngestionReason.INVALID_JOB
        ).model_dump(mode="json")

    job_try = int(ctx.get("job_try", 1))
    final_attempt = job_try >= int(ctx["ingest_max_tries"])

    async with get_sessionmaker()() as session:
        service = IngestionService(session, ctx["storage"])
        try:
            result = await service.ingest(job, final_attempt=final_attempt)
        except TransientIngestionError as exc:
            raise Retry(defer=retry_delay_seconds(job_try)) from exc
        except Exception as exc:
            # Unclassified — most likely the database. Retry while attempts
            # remain. On the last one there may be nothing left to record the
            # failure with; the claim, if any, is left for the reaper.
            if final_attempt:
                _log.error(
                    "ingestion.gave_up",
                    document_id=job.document_id,
                    error=type(exc).__name__,
                )
                raise
            _log.warning(
                "ingestion.retry",
                document_id=job.document_id,
                error=type(exc).__name__,
            )
            raise Retry(defer=retry_delay_seconds(job_try)) from exc
    return result.model_dump(mode="json")


async def reap_stale_processing(ctx: dict[str, Any]) -> list[str]:
    """Cron: fail documents whose claim has outlived any worker (ADR-0009 §6)."""
    async with get_sessionmaker()() as session:
        return await IngestionService(session, ctx["storage"]).reap_stale_processing(
            stale_after_seconds=int(ctx["stale_processing_seconds"])
        )
