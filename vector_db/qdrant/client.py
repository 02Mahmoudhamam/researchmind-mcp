"""Building the Qdrant client.

Not cached. The scaffold's `@lru_cache` on a no-argument factory did two things
wrong: it froze configuration at first call, and it shared one HTTP client —
and so one event loop's connection pool — across every caller. The client's
lifetime belongs to whoever owns the loop, which is the worker's `startup()`
and `shutdown()` (ADR-0009's composition root).
"""

from qdrant_client import AsyncQdrantClient

from vector_db.qdrant.config import QdrantConfig


def build_qdrant_client(config: QdrantConfig) -> AsyncQdrantClient:
    """An async client for `config`. Opens nothing until it is used."""
    return AsyncQdrantClient(
        host=config.host,
        port=config.port,
        timeout=config.timeout_seconds,
    )
