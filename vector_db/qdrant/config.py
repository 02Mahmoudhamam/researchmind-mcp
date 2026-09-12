"""Qdrant connection configuration."""

from pydantic import BaseModel
from backend.config.settings import get_settings

settings = get_settings()


class QdrantConfig(BaseModel):
    host: str = settings.QDRANT_HOST
    port: int = settings.QDRANT_PORT
    collection_name: str = settings.QDRANT_COLLECTION
    vector_size: int = 1536  # text-embedding-3-small dimension
    distance: str = "Cosine"
    timeout: int = 30
