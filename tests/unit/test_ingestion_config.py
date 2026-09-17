"""Ingestion worker configuration and retry pacing — no Redis, no database."""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.ingestion import worker as worker_module
from backend.ingestion.worker import RETRY_DELAYS_SECONDS, retry_delay_seconds
from shared.interfaces.embedding import SPECIAL_TOKENS_PER_SEQUENCE
from tests.doubles import StubEmbeddingProvider


def _settings(**overrides: Any) -> Settings:
    return Settings(ANTHROPIC_API_KEY="k", APP_ENV="development", **overrides)


class TestWorkerSettings:
    def test_the_defaults_are_consistent(self) -> None:
        settings = _settings()

        assert (
            settings.INGEST_STALE_PROCESSING_SECONDS
            > settings.INGEST_JOB_TIMEOUT_SECONDS
        )
        assert settings.INGEST_MAX_TRIES >= 1

    @pytest.mark.parametrize(
        "field",
        ["ARQ_MAX_JOBS", "INGEST_JOB_TIMEOUT_SECONDS", "INGEST_MAX_TRIES"],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_setting_is_refused(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            _settings(**{field: value})

    @pytest.mark.parametrize("stale", [100, 300])
    def test_a_stale_threshold_not_above_the_job_timeout_is_refused(
        self, stale: int
    ) -> None:
        """At or below the timeout, the reaper could fail a job still running."""
        with pytest.raises(ValidationError, match="must be greater than"):
            _settings(
                INGEST_JOB_TIMEOUT_SECONDS=300, INGEST_STALE_PROCESSING_SECONDS=stale
            )

    @pytest.mark.parametrize("parse", [300, 301])
    def test_a_parse_budget_not_below_the_job_timeout_is_refused(
        self, parse: int
    ) -> None:
        """ARQ would cancel the job first, and no reason would ever be recorded."""
        with pytest.raises(ValidationError, match="must be less than"):
            _settings(
                INGEST_JOB_TIMEOUT_SECONDS=300, INGEST_PARSE_TIMEOUT_SECONDS=parse
            )

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_parse_budget_that_permits_nothing_is_refused(self, value: int) -> None:
        with pytest.raises(ValidationError):
            _settings(INGEST_PARSE_TIMEOUT_SECONDS=value)

    def test_the_default_parse_budget_fits_inside_the_job(self) -> None:
        settings = _settings()

        assert 0 < settings.INGEST_PARSE_TIMEOUT_SECONDS
        assert (
            settings.INGEST_PARSE_TIMEOUT_SECONDS < settings.INGEST_JOB_TIMEOUT_SECONDS
        )

    def test_a_stale_threshold_above_the_timeout_is_accepted(self) -> None:
        settings = _settings(
            INGEST_JOB_TIMEOUT_SECONDS=300, INGEST_STALE_PROCESSING_SECONDS=301
        )
        assert settings.INGEST_STALE_PROCESSING_SECONDS == 301


class TestRetryPacing:
    def test_delays_back_off_and_then_hold(self) -> None:
        delays = [retry_delay_seconds(attempt) for attempt in range(1, 8)]

        assert delays == sorted(delays)
        assert delays[-1] == RETRY_DELAYS_SECONDS[-1]
        assert delays[0] == RETRY_DELAYS_SECONDS[0]

    @pytest.mark.parametrize("attempt", [0, -3])
    def test_a_nonsensical_attempt_number_still_gets_a_delay(
        self, attempt: int
    ) -> None:
        assert retry_delay_seconds(attempt) == RETRY_DELAYS_SECONDS[0]


@pytest.mark.embeddings
@pytest.mark.qdrant
class TestTheWorkerProcess:
    """These call the real `startup`, which since M3/S3.5 loads the embedding
    model and prepares the collection — so they need both, and are marked so a
    machine without them skips rather than fails."""

    async def test_startup_applies_the_logging_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`arq` configures no logging of ours, so LOG_FORMAT would never apply.

        And arq's own lines keep their own handler rather than also reaching
        the root handler configured here, which would print each one twice.
        """
        calls: list[bool] = []
        monkeypatch.setattr(
            worker_module, "configure_logging", lambda: calls.append(True)
        )
        monkeypatch.setattr(logging.getLogger("arq"), "propagate", True)

        await worker_module.startup({})

        assert calls == [True]
        assert logging.getLogger("arq").propagate is False

    async def test_startup_builds_the_parser_from_the_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One process per concurrent job; the budget and page cap from Settings."""
        from backend.config.settings import get_settings
        from document_processing.pdf_parser import PyMuPDFTextExtractor

        monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
        monkeypatch.setattr(logging.getLogger("arq"), "propagate", True)
        ctx: dict[str, Any] = {}
        await worker_module.startup(ctx)
        settings = get_settings()

        extractor = ctx["extractor"]
        assert isinstance(extractor, PyMuPDFTextExtractor)
        assert extractor._timeout == settings.INGEST_PARSE_TIMEOUT_SECONDS
        assert extractor._limiter.total_tokens == settings.ARQ_MAX_JOBS
        assert ctx["max_pdf_pages"] == settings.MAX_PDF_PAGES


class TestChunksMustFitWhatTheModelReads:
    """ADR-0013 §1. The guard whose absence is invisible.

    Over the model's input limit the tail of every long chunk is dropped and
    embedded as though it were not there — a valid-looking vector for text the
    model never saw. Nothing downstream can detect it, so the worker refuses to
    start.
    """

    @staticmethod
    def _provider() -> StubEmbeddingProvider:
        """bge-small's limit, on a double that satisfies the real protocol."""
        return StubEmbeddingProvider(model_id="test-model", max_input_tokens=512)

    def test_a_chunk_that_fits_is_allowed(self) -> None:
        worker_module.refuse_chunks_the_model_cannot_read(510, self._provider())

    def test_a_chunk_that_exactly_fills_the_budget_is_allowed(self) -> None:
        """510 + 2 special tokens == 512. The boundary is inclusive."""
        worker_module.refuse_chunks_the_model_cannot_read(
            512 - SPECIAL_TOKENS_PER_SEQUENCE, self._provider()
        )

    def test_one_token_over_the_budget_is_refused(self) -> None:
        """511 + 2 > 512: the special tokens are part of what must fit."""
        with pytest.raises(RuntimeError) as raised:
            worker_module.refuse_chunks_the_model_cannot_read(511, self._provider())
        assert "truncated" in str(raised.value)
        assert "512" in str(raised.value) and "test-model" in str(raised.value)

    def test_a_much_larger_chunk_size_is_refused(self) -> None:
        with pytest.raises(RuntimeError):
            worker_module.refuse_chunks_the_model_cannot_read(4096, self._provider())

    def test_the_shipped_configuration_fits(self) -> None:
        """CHUNK_SIZE_TOKENS=400 against bge-small's 512, with room to spare."""
        from backend.config.settings import get_settings

        worker_module.refuse_chunks_the_model_cannot_read(
            get_settings().CHUNK_SIZE_TOKENS, self._provider()
        )
