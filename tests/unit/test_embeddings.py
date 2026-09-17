"""The embedding provider and the tokenizer that sizes what it embeds (M3/S3.5).

Marked `embeddings`: these run against the **real** model, because the
properties that matter — its dimension, its input limit, its determinism, and
that its tokenizer is the one sizing chunks — are properties of that model and
not of a stand-in for it. A mock would assert only that the mock works.

ADR-0013 §1 and §4; ADR-0004; ADR-0005.
"""

import inspect
import unicodedata

import pytest

from document_processing.embedder import FastEmbedProvider, FastEmbedTokenizer
from document_processing.tokenization import TOKENIZER_ID, RegexTokenizer
from shared.interfaces.embedding import (
    SPECIAL_TOKENS_PER_SEQUENCE,
    EmbeddingError,
    EmbeddingProvider,
)
from shared.interfaces.tokenization import Tokenizer

pytestmark = pytest.mark.embeddings

PARAGRAPH = (
    "Section 3.1 Evaluation. We measure retrieval quality on a held-out set "
    "of queries, and report recall at ten alongside mean reciprocal rank."
)


class TestTheProviderDescribesItsModel:
    def test_it_reports_the_model_it_loaded(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        assert embedding_provider.model_id == "BAAI/bge-small-en-v1.5"

    def test_it_reports_the_dimension_the_collection_is_built_from(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """384 for bge-small — and the scaffold's hard-coded 1536 was wrong."""
        assert embedding_provider.dimension == 384

    def test_it_reports_the_input_limit_chunks_must_fit(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """Beyond it the model truncates silently, which is the failure to stop."""
        assert embedding_provider.max_input_tokens == 512

    def test_the_dimension_is_measured_not_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model whose output disagrees with its catalogue is a substitution.

        ADR-0005 forbids silently accepting one. The guard is what makes
        `dimension` an observation rather than a claim.
        """
        import document_processing.embedder as module

        monkeypatch.setattr(module, "_declared_dimension", lambda model_id: 1536)
        with pytest.raises(EmbeddingError) as raised:
            FastEmbedProvider("BAAI/bge-small-en-v1.5")
        assert "384" in str(raised.value) and "1536" in str(raised.value)

    def test_it_satisfies_the_protocol_the_service_depends_on(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        provider: EmbeddingProvider = embedding_provider
        assert provider.dimension > 0
        assert set(vars(EmbeddingProvider)) >= {"embed_documents", "embed_query"}
        assert list(
            inspect.signature(EmbeddingProvider.embed_documents).parameters
        ) == ["self", "texts"]

    def test_a_model_that_cannot_be_loaded_is_an_embedding_error(self) -> None:
        with pytest.raises(EmbeddingError) as raised:
            FastEmbedProvider("not-a-real/model-name")
        assert "could not be loaded" in str(raised.value)

    def test_a_non_positive_batch_size_is_refused(self) -> None:
        for size in (0, -1):
            with pytest.raises(ValueError):
                FastEmbedProvider("BAAI/bge-small-en-v1.5", batch_size=size)


class TestEmbedding:
    async def test_it_returns_one_vector_per_text_in_order(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        texts = ["first chunk", "second chunk", "third chunk"]
        vectors = await embedding_provider.embed_documents(texts)
        assert len(vectors) == 3
        assert all(len(v) == embedding_provider.dimension for v in vectors)
        alone = await embedding_provider.embed_documents(["second chunk"])
        assert vectors[1] == alone[0], "order must follow the input"

    async def test_vectors_are_plain_floats(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """No NumPy above this seam: the service stores and ships plain numbers."""
        (vector,) = await embedding_provider.embed_documents([PARAGRAPH])
        assert isinstance(vector, tuple)
        # `isinstance` would pass for numpy.float64, which is a float subclass.
        assert {type(value) for value in vector} == {float}

    async def test_the_same_text_always_gives_the_same_vector(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """Determinism is what makes re-embedding after a crash idempotent."""
        first = await embedding_provider.embed_documents([PARAGRAPH])
        second = await embedding_provider.embed_documents([PARAGRAPH])
        assert first == second

    async def test_batching_does_not_change_the_result(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """A document larger than one batch must embed to the same vectors."""
        texts = [f"chunk number {i}" for i in range(9)]
        small = FastEmbedProvider(embedding_provider.model_id, batch_size=2)
        assert await small.embed_documents(
            texts
        ) == await embedding_provider.embed_documents(texts)

    async def test_a_query_uses_the_same_model_as_the_documents(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """Otherwise retrieval compares vectors from two different spaces."""
        (document,) = await embedding_provider.embed_documents([PARAGRAPH])
        query = await embedding_provider.embed_query(PARAGRAPH)
        assert query == document

    async def test_vectors_are_unit_length(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """ADR-0013: normalised, so cosine and dot agree in the collection."""
        (vector,) = await embedding_provider.embed_documents([PARAGRAPH])
        assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-5)

    async def test_similar_text_scores_above_unrelated_text(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """A sanity check that the vectors mean something, not just that they exist."""
        related, unrelated, query = await embedding_provider.embed_documents(
            [
                "We evaluate retrieval quality using recall at ten.",
                "The cat sat on the mat in the afternoon sun.",
                "How is retrieval quality measured?",
            ]
        )

        def dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
            return sum(x * y for x, y in zip(a, b))

        assert dot(query, related) > dot(query, unrelated)

    async def test_nothing_to_embed_calls_no_model(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        assert await embedding_provider.embed_documents([]) == ()

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t", " "])
    async def test_empty_text_is_refused_rather_than_embedded(
        self, embedding_provider: FastEmbedProvider, empty: str
    ) -> None:
        """The model will happily embed whitespace. The vector is meaningless,
        and storing it would put an unsearchable point in the collection."""
        with pytest.raises(EmbeddingError):
            await embedding_provider.embed_documents(["real text", empty])
        with pytest.raises(EmbeddingError):
            await embedding_provider.embed_query(empty)


class TestFailuresLeakNothing:
    async def test_the_providers_exception_never_reaches_the_traceback(
        self, embedding_provider: FastEmbedProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`failure_reason` and the worker's logs are built from these errors."""
        secret = "onnx-session-/home/someone/.cache/token-abc123"

        def explode(*args: object, **kwargs: object) -> object:
            raise RuntimeError(secret)

        monkeypatch.setattr(embedding_provider._model, "embed", explode)
        with pytest.raises(EmbeddingError) as raised:
            await embedding_provider.embed_documents(["a chunk"])

        rendered = "".join(
            __import__("traceback").format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert secret not in rendered
        assert "RuntimeError" not in rendered

    async def test_the_message_names_no_text_that_was_embedded(
        self, embedding_provider: FastEmbedProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

        monkeypatch.setattr(embedding_provider._model, "embed", explode)
        with pytest.raises(EmbeddingError) as raised:
            await embedding_provider.embed_documents(["a confidential paragraph"])
        assert "confidential" not in str(raised.value)
        assert "1 text" in str(raised.value)

    async def test_a_vector_of_the_wrong_width_is_refused(
        self, embedding_provider: FastEmbedProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Qdrant would reject it too, but not before the chunk was recorded."""
        monkeypatch.setattr(
            embedding_provider._model, "embed", lambda *a, **k: iter([[0.1, 0.2]])
        )
        with pytest.raises(EmbeddingError) as raised:
            await embedding_provider.embed_documents(["a chunk"])
        assert "384" in str(raised.value)

    async def test_the_wrong_number_of_vectors_is_refused(
        self, embedding_provider: FastEmbedProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently zipping short would pair a chunk with another chunk's vector."""
        monkeypatch.setattr(
            embedding_provider._model, "embed", lambda *a, **k: iter([[0.0] * 384])
        )
        with pytest.raises(EmbeddingError) as raised:
            await embedding_provider.embed_documents(["one", "two"])
        assert "1 vectors" in str(raised.value) and "2 text" in str(raised.value)


class TestTheModelsTokenizerIsAuthoritative:
    @pytest.fixture()
    def tokenizer(self, embedding_provider: FastEmbedProvider) -> FastEmbedTokenizer:
        return embedding_provider.tokenizer

    def test_it_identifies_itself_by_model_and_vocabulary(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        """Written to every chunk, so a model change is a query (ADR-0013 §2)."""
        assert tokenizer.id == "bge-small-en-v1.5/wordpiece"

    def test_it_is_not_the_provisional_tokenizer(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        """S3.4's id must differ, or a re-chunk could never be detected."""
        assert tokenizer.id != TOKENIZER_ID == "regex-word/v1"

    def test_it_satisfies_the_protocol_the_chunker_depends_on(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        conforming: Tokenizer = tokenizer
        assert conforming.count("one two") == len(conforming.tokenize("one two"))

    def test_counting_is_not_capped_by_the_models_input_limit(
        self, tokenizer: FastEmbedTokenizer, embedding_provider: FastEmbedProvider
    ) -> None:
        """The bug this design exists to prevent.

        The model's own tokenizer truncates at 512. Counting a whole section
        with it would report 512 for a section of any length, and the chunker
        would cut one enormous chunk instead of many correct ones.
        """
        section = ("alpha beta gamma delta " * 400).strip()
        assert tokenizer.count(section) > embedding_provider.max_input_tokens
        assert tokenizer.count(section) == 1600

    def test_the_models_own_tokenizer_still_truncates(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """Our copy must not have reconfigured the instance the model embeds with."""
        inner = getattr(embedding_provider._model.model, "tokenizer")
        assert inner.truncation["max_length"] == 512

    def test_special_tokens_are_not_counted(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        """`[CLS]` and `[SEP]` carry no text, and are per-sequence overhead."""
        spans = tokenizer.tokenize("hello")
        assert spans == tokenizer.tokenize("hello")
        assert all(span.end > span.start for span in spans)
        assert tokenizer.count("hello") == 1

    def test_the_special_token_allowance_matches_what_the_model_adds(
        self, embedding_provider: FastEmbedProvider
    ) -> None:
        """The two the budget must leave room for, measured not assumed."""
        inner = getattr(embedding_provider._model.model, "tokenizer")
        encoding = inner.encode("hello")
        assert sum(encoding.special_tokens_mask) == SPECIAL_TOKENS_PER_SEQUENCE == 2

    def test_every_span_is_a_verbatim_substring(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        """Spans, not encode/decode: a citation must be findable in the page."""
        spans = tokenizer.tokenize(PARAGRAPH)
        assert [PARAGRAPH[s.start : s.end] for s in spans][:5] == [
            "Section",
            "3",
            ".",
            "1",
            "Evaluation",
        ]
        assert all(PARAGRAPH[s.start : s.end].strip() for s in spans)

    def test_spans_are_ordered_and_do_not_overlap(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        spans = tokenizer.tokenize(PARAGRAPH)
        assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))

    def test_it_is_deterministic(self, tokenizer: FastEmbedTokenizer) -> None:
        assert tokenizer.tokenize(PARAGRAPH) == tokenizer.tokenize(PARAGRAPH)

    @pytest.mark.parametrize(
        "text",
        [
            "研究論文の要旨",
            "Fußnote über Größe",
            "naïve café — résumé",
            unicodedata.normalize("NFD", "café"),
            "emoji 🧪 in prose",
        ],
    )
    def test_unicode_spans_map_back_to_the_original_text(
        self, tokenizer: FastEmbedTokenizer, text: str
    ) -> None:
        """Offsets are character offsets, so a chunk stays a substring."""
        for span in tokenizer.tokenize(text):
            assert text[span.start : span.end] == text[span.start : span.end].strip()
        assert tokenizer.count(text) >= 1

    def test_empty_text_has_no_tokens(self, tokenizer: FastEmbedTokenizer) -> None:
        assert tokenizer.tokenize("") == ()
        assert tokenizer.count("   ") == 0

    def test_it_counts_subwords_the_regex_tokenizer_cannot(
        self, tokenizer: FastEmbedTokenizer
    ) -> None:
        """The reason S3.4's counts were provisional: one word, several tokens."""
        word = "immunohistochemistry"
        assert RegexTokenizer().count(word) == 1
        assert tokenizer.count(word) > 1
