"""Application settings via pydantic-settings."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    # App
    APP_NAME: str = "ResearchMind MCP"
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    DEBUG: bool = False
    SECRET_KEY: str = "changeme"

    # Claude
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"

    # Database (PostgreSQL — the system of record, ADR-0003)
    #
    # Defaults to localhost, matching QDRANT_HOST and REDIS_HOST below: an
    # infrastructure endpoint for local development, not a secret. The
    # credentials are the development ones declared in docker-compose.yml.
    # Under compose the backend service overrides the host, because `postgres`
    # is only resolvable inside the compose network.
    DATABASE_URL: str = (
        "postgresql+asyncpg://researchmind:researchmind@localhost:5432/researchmind"
    )
    # Pool sizing for a single-node deployment. asyncpg holds one connection per
    # slot, so this caps at DB_POOL_SIZE + DB_MAX_OVERFLOW concurrent sessions.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    # Logs every statement. Useful when debugging a query, far too noisy
    # otherwise, and it will happily print values from a real database.
    DB_ECHO: bool = False

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "researchmind"

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_TTL: int = 86400

    # JWT
    JWT_SECRET: str = "changeme"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject a synchronous DSN before it reaches create_async_engine().

        `postgresql://` is the form every tutorial and connection string uses,
        and it is silently wrong here: SQLAlchemy resolves it to psycopg2, which
        is not installed and is not async. The resulting error surfaces deep
        inside engine creation and names neither this setting nor the fix. A
        configuration mistake should be reported by the configuration layer.
        """
        expected = "postgresql+asyncpg://"
        if not value.startswith(expected):
            raise ValueError(
                f"DATABASE_URL must use the asyncpg driver, i.e. start with "
                f"{expected!r} — got {value.split('://')[0] + '://'!r}. "
                f"This application uses an async engine (ADR-0003); a "
                f"synchronous DSN cannot drive it."
            )
        return value


@lru_cache()
def get_settings() -> Settings:
    return Settings()
