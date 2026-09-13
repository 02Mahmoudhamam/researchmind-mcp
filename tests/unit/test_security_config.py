"""Fail-closed security configuration.

security/principles.md §6: "The application **refuses to start** with default
`changeme` secrets outside `APP_ENV=development`." A system that boots with a
known signing key is worse than one that refuses — every token it issues is
forgeable by anyone who has read the repository, and nothing about its
behaviour says so.
"""

import pytest
from pydantic import ValidationError

from backend.config.settings import Settings

STRONG = "a-real-looking-signing-key-of-sufficient-length"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"ANTHROPIC_API_KEY": "test-key"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


class TestDevelopmentStaysUsable:
    def test_development_boots_with_the_placeholder_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`cp .env.example .env` must still work.

        The default exists precisely so it can be rejected elsewhere; making it
        required would mean inventing a secret before the app will start
        locally, which is friction that buys nothing.

        The environment is cleared first: conftest.py exports real-length
        secrets for the suite, so without this the test would read those and
        never see the default it is about.
        """
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)

        settings = _settings(APP_ENV="development")

        assert settings.SECRET_KEY == "changeme"
        assert settings.JWT_SECRET == "changeme"

    def test_the_test_suites_own_configuration_is_accepted(self) -> None:
        """conftest.py sets APP_ENV=test with real-length secrets."""
        assert (
            _settings(APP_ENV="test", SECRET_KEY=STRONG, JWT_SECRET=STRONG).APP_ENV
            == "test"
        )


class TestProductionFailsClosed:
    @pytest.mark.parametrize("environment", ["production", "staging", "test", "prod"])
    def test_the_placeholder_is_refused_outside_development(
        self, environment: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Everything that is not development must set real values.

        Environment cleared so the shipped default is what gets validated,
        rather than conftest.py's secrets.
        """
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)

        with pytest.raises(ValidationError, match="insecure secret configuration"):
            _settings(APP_ENV=environment)

    @pytest.mark.parametrize(
        "secret",
        ["changeme", "CHANGEME", "secret", "password", "your-secret-key-here", "test"],
    )
    def test_known_placeholder_values_are_refused(self, secret: str) -> None:
        with pytest.raises(ValidationError, match="known placeholder value"):
            _settings(APP_ENV="production", SECRET_KEY=secret, JWT_SECRET=STRONG)

    def test_an_empty_secret_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="is empty"):
            _settings(APP_ENV="production", SECRET_KEY="   ", JWT_SECRET=STRONG)

    def test_a_short_secret_is_refused(self) -> None:
        """32 characters. PyJWT warns below that for HS256 (RFC 7518 §3.2)."""
        with pytest.raises(ValidationError, match="shorter than 32"):
            _settings(APP_ENV="production", SECRET_KEY="a" * 31, JWT_SECRET=STRONG)

    def test_each_secret_is_checked_independently(self) -> None:
        """One strong key does not excuse the other."""
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            _settings(APP_ENV="production", SECRET_KEY=STRONG, JWT_SECRET="changeme")

    def test_strong_secrets_are_accepted(self) -> None:
        """The control. Without it, a validator that always raised would pass."""
        settings = _settings(APP_ENV="production", SECRET_KEY=STRONG, JWT_SECRET=STRONG)

        assert settings.APP_ENV == "production"


class TestErrorsDoNotLeakSecrets:
    def test_the_message_never_contains_a_secret_value(self) -> None:
        """An error that echoes a secret puts it in logs and issue reports."""
        leaked = "s3cret-value-nobody-should-ever-see-in-a-log"

        with pytest.raises(ValidationError) as excinfo:
            _settings(APP_ENV="production", SECRET_KEY=leaked, JWT_SECRET="changeme")

        assert leaked not in str(excinfo.value)

    def test_the_message_names_the_setting_and_the_problem(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _settings(APP_ENV="production", SECRET_KEY="changeme", JWT_SECRET=STRONG)

        message = str(excinfo.value)
        assert "SECRET_KEY" in message
        assert "placeholder" in message


class TestTokenLifetime:
    def test_the_default_ttl_is_sixty_minutes(self) -> None:
        """principles.md §6: access tokens only, 60 minutes, no refresh.

        There is no server-side revocation, so a stolen token is valid until it
        expires. That is the entire argument for a short TTL, and why this is
        pinned rather than left to taste.
        """
        assert _settings().JWT_EXPIRE_MINUTES == 60


class TestCorsSettings:
    def test_the_default_origin_matches_the_frontend_dev_server(self) -> None:
        assert _settings().CORS_ORIGINS == ["http://localhost:3000"]

    def test_a_comma_separated_value_becomes_a_list(self) -> None:
        """`.env` files hold strings, not JSON arrays.

        Without NoDecode, pydantic-settings tries to JSON-decode a list field
        from the environment and raises before any validator runs — so
        `CORS_ORIGINS=http://localhost:3000` would be a startup crash.
        """
        settings = _settings(CORS_ORIGINS="http://a.test, http://b.test")

        assert settings.CORS_ORIGINS == ["http://a.test", "http://b.test"]

    def test_a_single_origin_is_still_a_list(self) -> None:
        assert _settings(CORS_ORIGINS="http://only.test").CORS_ORIGINS == [
            "http://only.test"
        ]

    def test_an_empty_value_yields_no_origins(self) -> None:
        """Deny-all is the safe reading of "configured with nothing"."""
        assert _settings(CORS_ORIGINS="").CORS_ORIGINS == []
