"""Integration tests exercising the FastAPI application over ASGI.

These run against the real application object: routing, middleware and
response handling all execute. Nothing here is mocked, and no socket is opened.
"""

import pytest


async def test_health_endpoint_reports_liveness(api_client):
    """GET /health is reachable and reports the service is alive.

    Guards the wiring as much as the handler: before the health router was
    mounted in create_app(), this path returned 404.
    """
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_unknown_path_returns_404(api_client):
    """A path the application does not define must still 404.

    Without this, test_health_endpoint_reports_liveness could pass against an
    application that answered 200 to everything — a catch-all route, or an
    over-broad mount. This is the negative control that gives the positive
    assertion its meaning.
    """
    response = await api_client.get("/definitely-not-a-route")

    assert response.status_code == 404


async def test_health_is_not_under_the_api_version_prefix(api_client):
    """Liveness lives at the root, not under /api/v1.

    Infrastructure probes this path. Pinning it means a future API version bump
    cannot silently relocate the endpoint that container orchestration depends
    on.
    """
    assert (await api_client.get("/health")).status_code == 200
    assert (await api_client.get("/api/v1/health")).status_code == 404


@pytest.mark.xfail(
    strict=True,
    reason=(
        "/health/ready reports ready unconditionally without probing Qdrant, "
        "Redis or the Claude API. Real dependency checks land in Milestone M9 "
        "(Hardening & Observability)."
    ),
)
async def test_readiness_reports_dependency_status(api_client):
    """Readiness must describe what it actually checked.

    Mounted in this sprint alongside liveness, but its handler returns a fixed
    {"status": "ready", "dependencies": {}}. An orchestrator would read that as
    healthy while every backing service was down. Pinned strict so M9 cannot
    consider readiness finished while the payload is still empty.
    """
    response = await api_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()[
        "dependencies"
    ], "readiness must report the status of each checked dependency"
