"""The API's retrieval composition root — M4/S4.2.

One place builds the infrastructure retrieval needs, and one place closes it.
`backend/ingestion/worker.py`'s `startup()` is the same idea for the worker;
this is the API's.

ADR-0014 §11–§12 govern what happens here, and the asymmetry between them is
the whole design:

* **Own the resource.** An `EmbeddingProvider` and a Qdrant client are built
  once per process, eagerly, at startup. Never per request, never at import,
  never inside a route.
* **Do not probe the service.** Nothing here makes a network call.
  `AsyncQdrantClient` connects on first use, and `ensure_collection` is the
  **worker's** — calling it here would make a reachable Qdrant a condition of
  the API booting, which is precisely what `app.py`'s lifespan comment exists
  to prevent, and would take `/health` down with it.

The provider is process-local by design (ADR-0013's amendment). The API and the
worker hold different objects and coordinate through nothing; what keeps their
vectors comparable is the `embedding_model_id`/`dimension` predicate S4.1 put
in SQL, checked on every search rather than once at wiring time.

A plain function returning a plain dataclass, so a test can build one without a
FastAPI application and close it without a shutdown event.
"""

from dataclasses import dataclass

from backend.config.settings import Settings
from document_processing.embedder import FastEmbedProvider
from shared.interfaces.embedding import EmbeddingError, EmbeddingProvider
from shared.interfaces.vector_store import AsyncCloseable, VectorStore
from vector_db.qdrant.client import build_qdrant_client
from vector_db.qdrant.config import QdrantConfig
from vector_db.qdrant.repository import QdrantVectorStore


class RetrievalUnavailable(RuntimeError):
    """The API cannot serve search because its composition root is not built.

    Raised at **startup** when the provider will not load, so the process fails
    to boot rather than accepting requests it cannot answer (ADR-0014 §11); and
    at **request time** if a route somehow reaches the dependency without a
    lifespan having run, which is a wiring bug rather than an outage.
    """


@dataclass(frozen=True)
class RetrievalResources:
    """What one API process holds for the lifetime of the process."""

    embedder: EmbeddingProvider
    vectors: VectorStore
    # Typed as the seam, not as `AsyncQdrantClient`: nothing in `backend/api/`
    # imports a vendor module, and an architecture test holds that.
    client: AsyncCloseable

    async def aclose(self) -> None:
        """Release the Qdrant connection pool. The model has nothing to close."""
        await self.client.close()


def build_retrieval_resources(settings: Settings) -> RetrievalResources:
    """Load the embedding model and build the vector store client.

    Eager, and deliberately: 0.52 s at boot with warm weights is cheaper than a
    stall plus a first-request race under concurrency, and a provider that
    cannot load should stop the process serving rather than fail the first
    search (ADR-0014 §11).

    :raises RetrievalUnavailable: the model could not be loaded. The message
        names the model and nothing else — no path, no cache directory.
    """
    try:
        embedder: EmbeddingProvider = FastEmbedProvider(
            settings.EMBEDDING_MODEL,
            cache_dir=settings.EMBEDDING_CACHE_DIR,
            batch_size=settings.EMBEDDING_BATCH_SIZE,
        )
    except EmbeddingError as exc:
        raise RetrievalUnavailable(
            f"the embedding model {settings.EMBEDDING_MODEL!r} could not be loaded"
        ) from exc

    config = QdrantConfig.from_settings(settings)
    client = build_qdrant_client(config)
    # No `ensure_collection` here. See the module docstring and ADR-0014 §12.
    return RetrievalResources(
        embedder=embedder, vectors=QdrantVectorStore(client, config), client=client
    )
