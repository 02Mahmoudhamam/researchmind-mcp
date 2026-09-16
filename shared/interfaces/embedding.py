"""Turning text into vectors — the seam between ingestion and an embedding model.

ADR-0005: the dimension is a **property of the active provider**, never a
literal. The Qdrant collection is created from it, chunks record it, and a
mismatch between the two is a configuration error that must surface at boot
rather than under traffic.

`IngestionService` depends on this protocol, never on FastEmbed, ONNX or
NumPy: a provider takes text and returns plain floats, so the application layer
never handles a vendor object. An implementation must be deterministic — the
same text and model version always produce the same vector — and must say which
model it is, because a vector means nothing without knowing what made it.
"""

from typing import Protocol, Sequence

# A vector as the application passes it around: plain floats, never a tensor.
Vector = tuple[float, ...]


class EmbeddingError(Exception):
    """A provider could not embed. The message names no text and no credential."""


class EmbeddingProvider(Protocol):
    """Embeds document text and queries with one model."""

    # The model's identity, e.g. "BAAI/bge-small-en-v1.5". Recorded on every
    # chunk and every vector payload, so a model change is detectable in data.
    model_id: str

    # How many numbers a vector has. The collection is built from this.
    dimension: int

    # The most tokens the model reads before it silently truncates. Chunks are
    # sized to fit inside it (ADR-0013 §1); the worker refuses to start if they
    # are not.
    max_input_tokens: int

    async def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """Embed chunk text, in the order given, one vector each.

        :raises EmbeddingError: on empty input text, or if the model fails.
        """
        ...

    async def embed_query(self, text: str) -> Vector:
        """Embed a search query with the same model the documents used.

        :raises EmbeddingError: on empty input text, or if the model fails.
        """
        ...
