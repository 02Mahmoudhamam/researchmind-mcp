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

import logging
from typing import Any

from arq import Retry
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import get_settings
from backend.db.engine import dispose_engine
from backend.db.session import get_sessionmaker
from backend.ingestion.queue import PooledArqIngestionQueue
from backend.services.ingestion_service import IngestionService, TransientIngestionError
from backend.storage import LocalStorage
from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.embedder import FastEmbedProvider
from document_processing.pdf_parser import PyMuPDFTextExtractor
from shared.interfaces.chunking import DocumentChunker
from shared.interfaces.embedding import (
    SPECIAL_TOKENS_PER_SEQUENCE,
    EmbeddingProvider,
)
from shared.interfaces.pdf_extraction import PdfTextExtractor
from shared.interfaces.storage import Storage
from shared.interfaces.vector_store import VectorStore
from vector_db.qdrant.client import build_qdrant_client
from vector_db.qdrant.config import QdrantConfig
from vector_db.qdrant.repository import QdrantVectorStore
from shared.models.ingestion import (
    IngestionJob,
    IngestionOutcome,
    IngestionReason,
    IngestionResult,
)
from shared.utils.logger import configure_logging, get_logger

_log = get_logger(__name__)

# Delay before each retry of a transient failure, by attempt. Backoff, capped:
# the first retry comes quickly in case the fault was momentary; later ones
# give storage or the database room to recover.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (2, 10, 30, 60)

# The most `pending` documents one recovery pass considers, oldest first. A
# bound on the work a single minute's sweep does, not on what is recovered: the
# next pass continues where this one's oldest documents have moved on.
RECOVERY_BATCH_SIZE = 500


def retry_delay_seconds(job_try: int) -> int:
    """How long to wait before the retry that follows attempt `job_try`."""
    return RETRY_DELAYS_SECONDS[min(max(job_try, 1), len(RETRY_DELAYS_SECONDS)) - 1]


def refuse_chunks_the_model_cannot_read(
    chunk_size: int, provider: EmbeddingProvider
) -> None:
    """Fail startup if a chunk could not fit inside what the model reads.

    A chunk plus the special tokens the model adds to every sequence must fit
    `max_input_tokens`. Over that the model truncates, and the tail of every
    long chunk is embedded as though it were not there — silently, with a
    vector that looks perfectly valid. The worker refuses to start instead
    (ADR-0013 §1), because a configuration that quietly discards text is worse
    than one that will not boot.

    Its own function rather than four lines inside `startup`, so it can be
    asserted without a model, a database and a Redis.
    """
    budget = chunk_size + SPECIAL_TOKENS_PER_SEQUENCE
    if budget > provider.max_input_tokens:
        raise RuntimeError(
            f"CHUNK_SIZE_TOKENS={chunk_size} plus {SPECIAL_TOKENS_PER_SEQUENCE} "
            f"special tokens exceeds the {provider.max_input_tokens} tokens "
            f"{provider.model_id} reads; chunks would be truncated before they "
            "were embedded"
        )


async def startup(ctx: dict[str, Any]) -> None:
    """Build the worker's collaborators once per process.

    The composition root for the worker, as the dependency providers are for
    the API: the only place a concrete storage backend or PDF parser is named.
    Everything below receives `Storage` and `PdfTextExtractor`.

    Logging is configured here too. The API's entrypoint, `main.py`, applies
    LOG_FORMAT and LOG_LEVEL; the `arq` command applies neither, so without this
    the worker would ignore both.
    """
    configure_logging()
    # arq's CLI gives its own logger a handler. With a root handler now present
    # as well, every arq line would otherwise be printed twice.
    logging.getLogger("arq").propagate = False
    settings = get_settings()
    storage: Storage = LocalStorage(settings.STORAGE_ROOT)
    ctx["storage"] = storage
    # One extraction process per concurrent job at most, so a job never waits
    # for a process with its parse budget running (M3/S3.3).
    extractor: PdfTextExtractor = PyMuPDFTextExtractor(
        timeout_seconds=settings.INGEST_PARSE_TIMEOUT_SECONDS,
        max_concurrency=settings.ARQ_MAX_JOBS,
    )
    ctx["extractor"] = extractor
    # The embedding model, loaded once per worker process. Seconds of work and,
    # on a cold cache, a download — so it happens at startup, never inside a
    # job, and the image carries the weights (ADR-0004 §2).
    embedder: EmbeddingProvider = FastEmbedProvider(
        settings.EMBEDDING_MODEL,
        cache_dir=settings.EMBEDDING_CACHE_DIR,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
    )
    ctx["embedder"] = embedder

    refuse_chunks_the_model_cannot_read(settings.CHUNK_SIZE_TOKENS, embedder)

    # The chunker is stateless and pure. Since M3/S3.5 it counts with the
    # embedding model's own tokenizer, so the ruler that sizes a chunk is the
    # one that reads it (ADR-0013 §1). Named only here.
    chunker: DocumentChunker = SectionAwareChunker(
        embedder.tokenizer,
        chunk_size=settings.CHUNK_SIZE_TOKENS,
        chunk_overlap=settings.CHUNK_OVERLAP_TOKENS,
    )
    ctx["chunker"] = chunker
    ctx["strategy_version"] = STRATEGY_VERSION
    ctx["tokenizer_id"] = embedder.tokenizer.id

    # The vector store, and the collection built to this model's width. Asserted
    # at startup so a model change meets a clear error here rather than a
    # refused upsert inside a job (ADR-0005 §2).
    qdrant_config = QdrantConfig.from_settings(settings)
    client = build_qdrant_client(qdrant_config)
    ctx["qdrant_client"] = client
    vectors: VectorStore = QdrantVectorStore(client, qdrant_config)
    await vectors.ensure_collection(dimension=embedder.dimension)
    ctx["vectors"] = vectors

    ctx["max_pdf_pages"] = settings.MAX_PDF_PAGES
    ctx["ingest_max_tries"] = settings.INGEST_MAX_TRIES
    ctx["stale_processing_seconds"] = settings.INGEST_STALE_PROCESSING_SECONDS
    _log.info(
        "ingestion.worker.started",
        embedding_model_id=embedder.model_id,
        dimension=embedder.dimension,
        tokenizer_id=embedder.tokenizer.id,
        collection=qdrant_config.collection_name,
    )


def _service(ctx: dict[str, Any], session: AsyncSession) -> IngestionService:
    """The service over this job's session and the worker's collaborators."""
    return IngestionService(
        session,
        ctx["storage"],
        extractor=ctx["extractor"],
        chunker=ctx["chunker"],
        embedder=ctx["embedder"],
        vectors=ctx["vectors"],
        max_pages=int(ctx["max_pdf_pages"]),
        strategy_version=str(ctx["strategy_version"]),
        tokenizer_id=str(ctx["tokenizer_id"]),
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    client = ctx.get("qdrant_client")
    if client is not None:
        # Closed here because it was opened here: the client holds this loop's
        # connection pool, and this is the loop shutting down.
        await client.close()
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
        service = _service(ctx, session)
        try:
            result = await service.ingest(job, final_attempt=final_attempt)
        except TransientIngestionError as exc:
            raise Retry(defer=retry_delay_seconds(job_try)) from exc
        except Exception as exc:
            # Unclassified — most likely the database. Retry while attempts
            # remain. On the last one, record the failure if the database will
            # take it: a document left `pending` would otherwise be handed a
            # fresh job by the recovery sweep, and retried forever.
            if final_attempt:
                _log.error(
                    "ingestion.gave_up",
                    document_id=job.document_id,
                    error=type(exc).__name__,
                )
                await _record_given_up(ctx, job)
                raise
            _log.warning(
                "ingestion.retry",
                document_id=job.document_id,
                error=type(exc).__name__,
            )
            raise Retry(defer=retry_delay_seconds(job_try)) from exc
    return result.model_dump(mode="json")


async def _record_given_up(ctx: dict[str, Any], job: IngestionJob) -> None:
    """Best effort, in a fresh session: the one that failed may be unusable."""
    try:
        async with get_sessionmaker()() as session:
            await _service(ctx, session).record_given_up(job)
    except Exception as exc:
        # The database is still unreachable. The document stays as it is; the
        # reaper or the recovery sweep takes it once the database is back.
        _log.error(
            "ingestion.gave_up_unrecorded",
            document_id=job.document_id,
            error=type(exc).__name__,
        )


async def recover_pending_documents(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron: queue a job for `pending` documents that have none (M3/S3.3).

    Enqueues through the worker's own Redis connection, which ARQ provides as
    `ctx["redis"]`.
    """
    async with get_sessionmaker()() as session:
        result = await _service(ctx, session).recover_pending(
            PooledArqIngestionQueue(ctx["redis"]), limit=RECOVERY_BATCH_SIZE
        )
    return result.model_dump(mode="json")


async def reap_stale_processing(ctx: dict[str, Any]) -> list[str]:
    """Cron: fail documents whose claim has outlived any worker (ADR-0009 §6)."""
    async with get_sessionmaker()() as session:
        return await _service(ctx, session).reap_stale_processing(
            stale_after_seconds=int(ctx["stale_processing_seconds"])
        )
