"""Redis async client factory."""
import redis.asyncio as aioredis
from memory_system.redis.config import RedisConfig
from functools import lru_cache


@lru_cache()
def get_redis_client() -> aioredis.Redis:
    """Return a cached async Redis client."""
    config = RedisConfig()
    return aioredis.from_url(
        f"redis://{config.host}:{config.port}/{config.db}",
        encoding="utf-8",
        decode_responses=True,
    )
