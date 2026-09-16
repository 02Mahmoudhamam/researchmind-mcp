"""FastEmbed embeddings, and the tokenizer that sizes what they embed — M3/S3.5.

ADR-0004 chose local FastEmbed inference with `BAAI/bge-small-en-v1.5`;
ADR-0005 put it behind a provider protocol; ADR-0013 makes the model's own
tokenizer authoritative for chunking, so the ruler that sizes a chunk is the
one that reads it.

Both live here because they are one model: loading it twice would mean two ONNX
sessions and two vocabularies in a worker that needs one of each.

Nothing above this module sees FastEmbed, ONNX or NumPy — `EmbeddingProvider`
returns plain floats, and `Tokenizer` returns character spans.
"""

from typing import Any, Sequence

import anyio.to_thread
from fastembed import TextEmbedding

from shared.interfaces.embedding import EmbeddingError, Vector  # noqa: F401
from shared.interfaces.tokenization import TokenSpan

# The tokenizer's id is the model's, made short and marked with what it is. It
# is written to every chunk, so it must fit `document_chunks.tokenizer_id`.
_TOKENIZER_SUFFIX = "wordpiece"


def _tokenizer_id_for(model_id: str) -> str:
    """ "BAAI/bge-small-en-v1.5" -> "bge-small-en-v1.5/wordpiece"."""
    return f"{model_id.rsplit('/', 1)[-1]}/{_TOKENIZER_SUFFIX}"


class FastEmbedTokenizer:
    """`Tokenizer` over the embedding model's own vocabulary (ADR-0013 §1).

    Built from a **copy** of the model's tokenizer with truncation off. The
    model's instance truncates at its input limit, which is right for embedding
    and wrong for counting: a section of three thousand tokens would be
    reported as 512, and the chunker would size one enormous chunk from it.
    Copying leaves the model's own instance exactly as FastEmbed configured it.

    Special tokens are not counted. They are per-sequence overhead, and they
    carry no text to chunk — `[CLS]` and `[SEP]` both report the span `(0, 0)`.
    """

    def __init__(self, tokenizer: Any, *, model_id: str) -> None:
        self._tokenizer = tokenizer
        self.id = _tokenizer_id_for(model_id)

    def tokenize(self, text: str) -> tuple[TokenSpan, ...]:
        encoding = self._tokenizer.encode(text)
        return tuple(
            TokenSpan(start, end)
            for (start, end), special in zip(
                encoding.offsets, encoding.special_tokens_mask
            )
            if not special and end > start
        )

    def count(self, text: str) -> int:
        return len(self.tokenize(text))


class FastEmbedProvider:
    """`EmbeddingProvider` over a locally-run FastEmbed model.

    The model is loaded once, at construction — in the worker's startup, not in
    a job — because loading it costs seconds and, on a cold cache, a download.
    ADR-0004 §2 bakes the weights into the image so that download never happens
    while a document is waiting.
    """

    def __init__(
        self,
        model_id: str,
        *,
        cache_dir: str | None = None,
        batch_size: int = 32,
    ) -> None:
        """
        :param model_id: the FastEmbed model name, e.g. "BAAI/bge-small-en-v1.5".
        :param cache_dir: where the weights live; None leaves FastEmbed its own.
        :param batch_size: chunks embedded per call into the model.
        :raises EmbeddingError: if the model, its tokenizer or its dimension
            cannot be established. Constructing this costs seconds and, on a
            cold cache, a download, so it belongs in a worker's startup.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        try:
            self._model = TextEmbedding(model_name=model_id, cache_dir=cache_dir)
        except Exception as exc:  # the model name, the cache, the download
            raise EmbeddingError(
                f"the embedding model {model_id!r} could not be loaded"
            ) from exc

        self.model_id = model_id
        self._batch_size = batch_size

        # FastEmbed keeps the tokenizer on the ONNX wrapper it builds. Reading
        # it is reaching one layer in, so it is done once, here, where a
        # FastEmbed upgrade that moves it fails loudly at startup instead of
        # quietly changing how chunks are sized.
        inner = getattr(self._model, "model", None)
        tokenizer = getattr(inner, "tokenizer", None)
        if tokenizer is None:
            raise EmbeddingError(
                f"FastEmbed exposes no tokenizer for {model_id!r}; "
                "chunk sizing cannot be made authoritative"
            )
        truncation = getattr(tokenizer, "truncation", None) or {}
        self.max_input_tokens = int(truncation.get("max_length", 0))
        if self.max_input_tokens <= 0:
            raise EmbeddingError(
                f"the tokenizer for {model_id!r} declares no input limit; "
                "a chunk could be truncated without anything noticing"
            )
        self.tokenizer = FastEmbedTokenizer(
            _untruncated_copy(tokenizer), model_id=model_id
        )

        # The dimension is **measured**, not taken on trust (ADR-0005 §2): the
        # catalogue describes the model, a probe describes this session. A
        # quantized or swapped variant that disagrees with its own catalogue
        # entry is a silent substitution, and it stops here rather than filling
        # a collection with vectors of the wrong width.
        self.dimension = len(self._run_model(("dimension probe",))[0])
        declared = _declared_dimension(model_id)
        if declared is not None and declared != self.dimension:
            raise EmbeddingError(
                f"{model_id!r} produced {self.dimension} numbers where its "
                f"catalogue declares {declared}"
            )

    async def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """Embed each text, in order. Runs off the event loop."""
        if not texts:
            return ()
        self._refuse_empty(texts)
        return await anyio.to_thread.run_sync(self._embed, tuple(texts))

    async def embed_query(self, text: str) -> Vector:
        """Embed one query with the same model the documents used (ADR-0005 §5)."""
        self._refuse_empty([text])
        vectors = await anyio.to_thread.run_sync(self._embed, (text,))
        return vectors[0]

    # --- the model ------------------------------------------------------------

    def _embed(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        vectors = self._run_model(texts)
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingError(
                    f"the embedding model returned {len(vector)} numbers, "
                    f"not the {self.dimension} it produced at startup"
                )
        return vectors

    def _run_model(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        """The model itself. Floats out, so no NumPy array leaves this module."""
        try:
            vectors = tuple(
                tuple(float(value) for value in vector)
                for vector in self._model.embed(
                    list(texts), batch_size=self._batch_size
                )
            )
        except Exception:
            # Never the text, never a path, and never the provider's own
            # exception: this message reaches a job's logs and `failure_reason`.
            raise EmbeddingError(
                f"the embedding model failed on {len(texts)} text(s)"
            ) from None
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"the embedding model returned {len(vectors)} vectors "
                f"for {len(texts)} text(s)"
            )
        return vectors

    @staticmethod
    def _refuse_empty(texts: Sequence[str]) -> None:
        """Embedding whitespace yields a vector that means nothing, and is stored."""
        if any(not text or not text.strip() for text in texts):
            raise EmbeddingError("a text to embed was empty")


def _untruncated_copy(tokenizer: Any) -> Any:
    """The same vocabulary, with truncation and padding switched off."""
    from tokenizers import Tokenizer as HuggingFaceTokenizer

    copy = HuggingFaceTokenizer.from_str(tokenizer.to_str())
    copy.no_truncation()
    copy.no_padding()
    return copy


def _declared_dimension(model_id: str) -> int | None:
    """What FastEmbed's catalogue says this model's width is, if it lists it."""
    for description in TextEmbedding.list_supported_models():
        if description.get("model") == model_id:
            dim = description.get("dim")
            return int(dim) if isinstance(dim, int) else None
    return None
