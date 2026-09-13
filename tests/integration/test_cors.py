"""CORS behaviour of the real application.

Asserted through the middleware rather than by reading settings: the question
is what a browser is told, and the only way to know that is to look at the
response headers.

No database — these exercise the ASGI app only.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from backend.api.app import create_app
from backend.config.settings import get_settings

ALLOWED = "http://localhost:3000"
FOREIGN = "https://evil.example"


def _app(monkeypatch: pytest.MonkeyPatch, origins: str | None = None) -> FastAPI:
    """Build a fresh app so a changed CORS setting actually takes effect."""
    if origins is not None:
        monkeypatch.setenv("CORS_ORIGINS", origins)
    get_settings.cache_clear()
    return create_app()


async def _get(app: FastAPI, headers: dict[str, str]) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/health", headers=headers)


class TestAllowedOrigins:
    async def test_the_configured_origin_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = await _get(_app(monkeypatch), {"Origin": ALLOWED})

        assert response.headers.get("access-control-allow-origin") == ALLOWED

    async def test_an_unconfigured_origin_is_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The browser must receive no allow header at all.

        Not a wildcard, not the requesting origin echoed back — absent.
        """
        response = await _get(_app(monkeypatch), {"Origin": FOREIGN})

        assert "access-control-allow-origin" not in response.headers

    async def test_the_origin_is_never_echoed_as_a_wildcard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`*` with Authorization is how a token gets used from any page."""
        response = await _get(_app(monkeypatch), {"Origin": ALLOWED})

        assert response.headers.get("access-control-allow-origin") != "*"

    async def test_multiple_configured_origins_are_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch, f"{ALLOWED},https://app.example")

        assert (await _get(app, {"Origin": ALLOWED})).headers.get(
            "access-control-allow-origin"
        ) == ALLOWED
        assert (await _get(app, {"Origin": "https://app.example"})).headers.get(
            "access-control-allow-origin"
        ) == "https://app.example"

    async def test_no_configured_origins_means_none_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = await _get(_app(monkeypatch, ""), {"Origin": ALLOWED})

        assert "access-control-allow-origin" not in response.headers


class TestCredentials:
    async def test_credentials_are_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The frontend sends a bearer header, not a cookie.

        `frontend/src/lib/api.ts` reads the token from localStorage and sets
        Authorization itself, so credentialed CORS buys nothing — and it is the
        setting that turns a permissive origin policy into a real one.
        """
        response = await _get(_app(monkeypatch), {"Origin": ALLOWED})

        assert "access-control-allow-credentials" not in response.headers


class TestPreflight:
    async def test_a_preflight_from_the_configured_origin_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.options(
                "/api/v1/documents/",
                headers={
                    "Origin": ALLOWED,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == ALLOWED
        assert (
            "authorization"
            in response.headers.get("access-control-allow-headers", "").lower()
        )

    async def test_a_preflight_from_an_unknown_origin_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.options(
                "/api/v1/documents/",
                headers={
                    "Origin": FOREIGN,
                    "Access-Control-Request-Method": "GET",
                },
            )

        assert "access-control-allow-origin" not in response.headers

    async def test_methods_are_enumerated_rather_than_wildcarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.options(
                "/api/v1/documents/",
                headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"},
            )

        allowed = response.headers.get("access-control-allow-methods", "")
        assert allowed and "*" not in allowed


class TestConfigurationSource:
    def test_the_application_does_not_hard_code_a_wildcard(self) -> None:
        """Guards the regression directly: `allow_origins=["*"]` was the code."""
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[2] / "backend" / "api" / "app.py"
        ).read_text(encoding="utf-8")

        assert 'allow_origins=["*"]' not in source
        assert "settings.CORS_ORIGINS" in source
