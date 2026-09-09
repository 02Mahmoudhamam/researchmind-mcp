"""Qdrant client factory and connection management."""
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams
from vector_db.qdrant.config import QdrantConfig
from functools import lru_cache


@lru_cache()
def get_qdrant_client() -> AsyncQdrantClient:
    """Return a cached Qdrant async client."""
    config = QdrantConfig()
    return AsyncQdrantClient(host=config.host, port=config.port, timeout=config.timeout)


async def ensure_collection_exists(client: AsyncQdrantClient, config: QdrantConfig) -> None:
    """Create the Qdrant collection if it does not exist."""
    ...  # TODO: implement with VectorParams
