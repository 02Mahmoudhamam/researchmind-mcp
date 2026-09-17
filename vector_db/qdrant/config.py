"""Where Qdrant is, and what collection to use.

Read from `Settings` **when asked**, not at import. The scaffold bound
`settings.QDRANT_HOST` as a class-attribute default, which froze the value at
the first import and defeated per-test overrides — the pattern
`backend/db/engine.py` deliberately avoids, and CLAUDE.md records as a trap.

There is **no `vector_size` here.** The scaffold hard-coded 1536 to match an
OpenAI model this project does not use. ADR-0005 makes the dimension a property
of the active embedding provider, so the collection is created from
`provider.dimension` and a literal cannot drift from the model.
"""

from dataclasses import dataclass

from backend.config.settings import Settings


@dataclass(frozen=True)
class QdrantConfig:
    """A resolved Qdrant location. Frozen, so nothing rebinds it mid-run."""

    host: str
    port: int
    collection_name: str
    timeout_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "QdrantConfig":
        return cls(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
            collection_name=settings.QDRANT_COLLECTION,
            timeout_seconds=settings.QDRANT_TIMEOUT_SECONDS,
        )

    @property
    def location(self) -> str:
        """Host and port, for a log line. Never a credential."""
        return f"{self.host}:{self.port}"
