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

from shared.interfaces.tokenization import Tokenizer

# A vector as the application passes it around: plain floats, never a tensor.
Vector = tuple[float, ...]

# How many tokens a model adds to every sequence of its own accord — `[CLS]`
# and `[SEP]` for the BERT-family vocabularies ADR-0004 selects from. They carry
# no text, so they are excluded from chunk and query token counts, and they are
# added back when checking that either fits `max_input_tokens`.
#
# Here rather than beside a provider, because both sides of this seam need it:
# the worker sizes chunks with it, and retrieval sizes queries with it — and
# retrieval must not import a vendor module to learn a number (ADR-0014).
# Asserted against the real tokenizer in `tests/unit/test_embeddings.py`.
SPECIAL_TOKENS_PER_SEQUENCE = 2


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

    @property
    def tokenizer(self) -> Tokenizer:
        """The model's **own** tokenizer — what sizes the chunks it will embed.

        On the protocol rather than on one implementation, because a provider
        whose tokenizer nobody can reach forces the chunker back onto a
        stand-in, which is exactly the situation ADR-0012 §2 recorded as
        provisional and ADR-0013 §1 ends.

        Read-only: the provider owns it, and a caller who could swap it could
        make the ruler disagree with the model again.
        """
        ...

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
