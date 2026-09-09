"""Redis connection configuration."""
from pydantic import BaseModel
from backend.config.settings import get_settings

settings = get_settings()


class RedisConfig(BaseModel):
    host: str = settings.REDIS_HOST
    port: int = settings.REDIS_PORT
    db: int = settings.REDIS_DB
    default_ttl: int = settings.REDIS_TTL
    key_prefix: str = "researchmind"
