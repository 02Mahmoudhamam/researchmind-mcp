"""Application settings via pydantic-settings."""

from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from functools import lru_cache

# Values a signing key must never have outside development. Not an exhaustive
# blocklist — a short list of what people actually leave in place, plus a length
# floor, catches the realistic mistake. The point is to fail loudly on the
# placeholder this repository shipped, not to score secret entropy.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "changethis",
        "secret",
        "password",
        "your-secret-key-here",
        "your-jwt-secret",
        "test",
        "dev",
        "development",
        "production",
    }
)

# HS256 keys should carry at least as much entropy as the digest they produce.
_MINIMUM_SECRET_LENGTH = 32

# Only symmetric HMAC algorithms. This is what makes JWT_ALGORITHM safe to read
# from configuration at all: without the allowlist, `JWT_ALGORITHM=none` would
# disable signature verification, and an asymmetric value would open the
# public-key-as-HMAC-secret confusion this project explicitly pins against.
_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    # App
    APP_NAME: str = "ResearchMind MCP"
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    DEBUG: bool = False
    # Still defaulted, and still refused outside development by
    # _reject_weak_secrets_outside_development below. security/principles.md is
    # worded as "refuses to start with default `changeme` secrets outside
    # APP_ENV=development", which requires the default to exist in order to be
    # rejected — a developer running locally should not need to invent a secret
    # before the application will boot.
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

    # Document storage (ADR-0008) and upload validation (principles.md §4)
    #
    # Relative, so it resolves against the working directory: `/app` in the
    # backend image, where docker-compose.yml mounts `./uploads:/app/uploads`,
    # and the repository root for local development, where `/uploads/` is
    # gitignored. Tests point it at a temporary directory (tests/conftest.py).
    STORAGE_ROOT: str = "uploads"
    # 50 MiB and 500 pages. Named by docs/development/environment.md with no
    # values, so these are chosen rather than inherited: comfortably above a
    # research paper or a thesis, well below what a single ingestion job should
    # be asked to hold in memory. Both are limits on what the system accepts,
    # not on what PDFs exist, so an operator with longer documents raises them.
    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024
    MAX_PDF_PAGES: int = 500

    # Ingestion worker (ADR-0009)
    #
    # ARQ_MAX_JOBS and INGEST_JOB_TIMEOUT_SECONDS are the names
    # docs/development/environment.md reserved for M3. The other two arrive with
    # the behaviour that needs them:
    #
    # INGEST_MAX_TRIES bounds retries of *transient* failures — ADR-0009 §5,
    # "never infinite retry". The last attempt records a terminal failure
    # instead of retrying again.
    #
    # INGEST_STALE_PROCESSING_SECONDS is how long a document may sit in
    # `processing` before the reaper declares its worker gone (ADR-0009 §6). It
    # must exceed the job timeout: ARQ cancels a job at the timeout, so anything
    # older has no live worker — and anything younger might.
    #
    # INGEST_PARSE_TIMEOUT_SECONDS (M3/S3.3) is the budget for extracting a PDF's
    # text, inside the job's own timeout: past it the parsing process is killed
    # and the document fails as `pdf_timeout`. It must leave the job time to read,
    # verify and persist around it — below the job timeout, or ARQ would cancel
    # the job first and the document would never record why.
    ARQ_MAX_JOBS: int = 10
    INGEST_JOB_TIMEOUT_SECONDS: int = 300
    INGEST_MAX_TRIES: int = 5
    INGEST_STALE_PROCESSING_SECONDS: int = 900
    INGEST_PARSE_TIMEOUT_SECONDS: int = 180

    # Chunking (M3/S3.4, ADR-005 §3 and §6, ADR-0012)
    #
    # ADR-005 fixes the shape: chunk "at ~400 tokens with ~15% overlap", counted
    # in tokens rather than characters — the ambiguity that ADR exists to end.
    # Tokens are counted by the `Tokenizer` the chunker is given, currently the
    # provisional `regex-word/v1`, so these are its tokens and not an embedding
    # model's; M4 revisits both together.
    CHUNK_SIZE_TOKENS: int = 400
    CHUNK_OVERLAP_TOKENS: int = 60

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "researchmind"

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_TTL: int = 86400

    # CORS
    #
    # Comma-separated in the environment, a list here. `NoDecode` is required:
    # without it pydantic-settings tries to JSON-decode a list field from the
    # environment and raises SettingsError *before* any validator runs, so
    # `CORS_ORIGINS=http://localhost:3000` would be a startup crash rather than
    # a value. Verified against pydantic-settings 2.15.
    #
    # The default matches the frontend's dev server and its published compose
    # port (frontend/package.json `next dev`, docker-compose.yml "3000:3000").
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # JWT
    JWT_SECRET: str = "changeme"
    JWT_ALGORITHM: str = "HS256"
    # 60 minutes, per security/principles.md §6: access tokens only, no refresh
    # token in the MVP, and no server-side revocation — so a stolen token is
    # valid until it expires. That is the whole argument for a short TTL.
    JWT_EXPIRE_MINUTES: int = 60

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def _require_symmetric_algorithm(cls, value: str) -> str:
        """Constrain the configured algorithm to a safe allowlist.

        The decode call passes this value explicitly rather than letting PyJWT
        read `alg` from the token header — that is algorithm confusion. Bounding
        it here is what lets the setting stay configurable without becoming a
        way to turn verification off.
        """
        if value not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"JWT_ALGORITHM must be one of "
                f"{sorted(_ALLOWED_JWT_ALGORITHMS)}, got {value!r}"
            )
        return value

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept `a,b` from the environment as well as a real list."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _reject_weak_secrets_outside_development(self) -> "Settings":
        """Refuse to start with a placeholder signing key (principles.md §6).

        A system that boots with a known signing key is worse than one that
        refuses to boot: every token it issues is forgeable by anyone who has
        read the repository, and nothing about its behaviour says so.

        Development is exempt so `cp .env.example .env` still works; everything
        else — staging, CI-with-real-config, production — must set real values.

        The message names the setting and the problem, never the value. An
        error that echoes a secret puts it in logs, terminals and issue reports.
        """
        if self.APP_ENV.strip().lower() == "development":
            return self

        problems: list[str] = []
        for name in ("SECRET_KEY", "JWT_SECRET"):
            value: str = getattr(self, name)
            if not value.strip():
                problems.append(f"{name} is empty")
            elif value.strip().lower() in _PLACEHOLDER_SECRETS:
                problems.append(f"{name} is a known placeholder value")
            elif len(value) < _MINIMUM_SECRET_LENGTH:
                problems.append(
                    f"{name} is shorter than {_MINIMUM_SECRET_LENGTH} characters"
                )

        if problems:
            raise ValueError(
                f"insecure secret configuration for APP_ENV={self.APP_ENV!r}: "
                + "; ".join(problems)
                + ". Set real values; see docs/development/environment.md."
            )
        return self

    @field_validator(
        "ARQ_MAX_JOBS",
        "INGEST_JOB_TIMEOUT_SECONDS",
        "INGEST_MAX_TRIES",
        "INGEST_STALE_PROCESSING_SECONDS",
        "INGEST_PARSE_TIMEOUT_SECONDS",
    )
    @classmethod
    def _require_positive_worker_setting(cls, value: int) -> int:
        """Zero jobs, zero tries or a zero timeout is a worker that does nothing."""
        if value <= 0:
            raise ValueError("ingestion worker settings must be positive integers")
        return value

    @model_validator(mode="after")
    def _stale_threshold_must_outlive_the_job_timeout(self) -> "Settings":
        """The reaper must never be able to fail a job that is still running.

        ARQ cancels a job once it has run for INGEST_JOB_TIMEOUT_SECONDS, so a
        document still `processing` after longer than that has no worker left.
        A threshold at or below the timeout would let the reaper fail documents
        whose worker is alive and within its budget.
        """
        if self.INGEST_STALE_PROCESSING_SECONDS <= self.INGEST_JOB_TIMEOUT_SECONDS:
            raise ValueError(
                "INGEST_STALE_PROCESSING_SECONDS must be greater than "
                "INGEST_JOB_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _parse_budget_must_fit_inside_the_job_timeout(self) -> "Settings":
        """A document that takes too long to parse must fail *as* too long.

        Parsing runs inside the job. If its budget reached the job timeout, ARQ
        would cancel the job before the budget ran out: the claim would be put
        back, no reason recorded, and the recovery sweep would queue the same
        hostile PDF again, and again.
        """
        if self.INGEST_PARSE_TIMEOUT_SECONDS >= self.INGEST_JOB_TIMEOUT_SECONDS:
            raise ValueError(
                "INGEST_PARSE_TIMEOUT_SECONDS must be less than "
                "INGEST_JOB_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _chunks_must_advance(self) -> "Settings":
        """A window must be positive, and must move.

        An overlap at or above the chunk size would re-read the same tokens for
        ever: the chunker steps `size - overlap` tokens, so a step of zero or
        less is not a smaller step, it is a loop.
        """
        if self.CHUNK_SIZE_TOKENS <= 0:
            raise ValueError("CHUNK_SIZE_TOKENS must be positive")
        if self.CHUNK_OVERLAP_TOKENS < 0:
            raise ValueError("CHUNK_OVERLAP_TOKENS must not be negative")
        if self.CHUNK_OVERLAP_TOKENS >= self.CHUNK_SIZE_TOKENS:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be less than CHUNK_SIZE_TOKENS")
        return self

    @field_validator("MAX_UPLOAD_BYTES", "MAX_PDF_PAGES")
    @classmethod
    def _require_positive_limit(cls, value: int) -> int:
        """A limit of zero or less would refuse every upload, or none.

        Zero refuses everything, which looks like an outage; a negative number
        compared against a length is True for every file, which looks like the
        limit is working. Neither is a configuration anyone means.
        """
        if value <= 0:
            raise ValueError("upload limits must be positive integers")
        return value

    @field_validator("STORAGE_ROOT")
    @classmethod
    def _require_storage_root(cls, value: str) -> str:
        """An empty root would resolve to the working directory itself.

        That is the repository checkout in development and `/app` in the image —
        blobs written among source files, and a key collision away from
        overwriting one.
        """
        if not value.strip():
            raise ValueError("STORAGE_ROOT must not be empty")
        return value

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
