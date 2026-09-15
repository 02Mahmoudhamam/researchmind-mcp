"""Ingestion worker configuration and retry pacing — no Redis, no database."""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.ingestion import worker as worker_module
from backend.ingestion.worker import RETRY_DELAYS_SECONDS, retry_delay_seconds


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


class TestTheWorkerProcess:
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
