"""Database connection dependencies."""

from vector_db.qdrant.client import get_qdrant_client
from memory_system.redis.client import get_redis_client


async def get_vector_db():
    """FastAPI dependency for Qdrant client."""
    yield get_qdrant_client()


async def get_memory_store():
    """FastAPI dependency for Redis client."""
    yield get_redis_client()
