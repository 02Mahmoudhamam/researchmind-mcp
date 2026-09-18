"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from backend.config.settings import get_settings
from backend.api.composition import build_retrieval_resources
from backend.db.engine import dispose_engine
from backend.api.routers import documents, agents, search, auth, workspace, health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the process-scoped resources retrieval needs; release them after.

    Startup **connects to nothing** (ADR-0014 §12). It loads the embedding
    model from the image's own disk and constructs a Qdrant client, neither of
    which touches the network — `AsyncQdrantClient` connects on first use. The
    database engine still connects lazily, and the Qdrant collection is still
    the worker's to create.

    That distinction is the design, not an omission. Making a reachable
    PostgreSQL or Qdrant a condition of booting would turn a momentarily
    unavailable dependency into a crash-loop instead of failing requests — and
    ``/health`` could no longer answer, which is exactly when a liveness probe
    matters most.

    What *is* a condition of booting is the model, because a provider that
    cannot load cannot serve a single search, and a process that cannot serve
    should not accept requests (ADR-0014 §11). It is loaded once here rather
    than per request: ~0.52 s and ~218 MB, paid at boot.

    Shutdown closes the Qdrant pool and disposes the database pool if one was
    ever created, so a restarting container leaves no sockets open.

    Redis is not held here: its client is a separate `@lru_cache`d factory with
    no shutdown hook wired anywhere yet, and it is the worker's dependency
    rather than the API's. Milestone M9 owns that.
    """
    resources = build_retrieval_resources(get_settings())
    app.state.retrieval = resources
    try:
        yield
    finally:
        # Cleared as well as closed. Leaving a closed client on `app.state`
        # would let a request after shutdown resolve a pool that is gone, and
        # fail somewhere far from the cause.
        del app.state.retrieval
        await resources.aclose()
        await dispose_engine()


# Field names whose submitted value must never be echoed back.
#
# FastAPI's default 422 handler includes the rejected input in the error, which
# is genuinely useful for `"year": "twenty"` and exactly wrong for a password:
# a registration with a password the policy rejects would return that password
# in the response body, from where it reaches proxy logs, browser devtools and
# error trackers. Caught by a test in tests/integration/test_registration_login.py.
_SENSITIVE_FIELD_FRAGMENTS = ("password", "secret", "token")


def _is_sensitive(location: object) -> bool:
    return any(
        fragment in str(part).lower()
        for part in (location if isinstance(location, (list, tuple)) else ())
        for fragment in _SENSITIVE_FIELD_FRAGMENTS
    )


async def _validation_error_without_secrets(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI's 422, with the submitted value removed for secret fields.

    Deliberately narrow. It changes *which value* is reported, never the shape
    of the response: the body is still `{"detail": [...]}` with the same
    `type`, `loc` and `msg`, so a client can still tell the caller which field
    was wrong and why. Only the echo of what they sent is replaced.
    """
    errors = [
        {**error, "input": "<redacted>"} if _is_sensitive(error.get("loc")) else error
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))


def create_app() -> FastAPI:
    # Read inside the factory, not at import. A module-level `get_settings()`
    # freezes configuration the moment anything imports this module — which is
    # how `QdrantConfig` and `RedisConfig` became untestable — and it means a
    # test that overrides settings has already lost.
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        docs_url="/docs" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # An explicit origin list, not "*". Once Authorization means something, a
    # wildcard lets any page a user visits drive this API with their token.
    #
    # allow_credentials stays False on purpose: the frontend sends a bearer
    # header (frontend/src/lib/api.ts), not a cookie, so credentialed CORS buys
    # nothing — and enabling it would be the thing that makes a wildcard
    # genuinely dangerous rather than merely wrong.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_exception_handler(
        RequestValidationError,
        _validation_error_without_secrets,  # type: ignore[arg-type]
    )

    # Mounted at the root, without a version prefix: liveness and readiness are
    # probed by infrastructure (containers, load balancers, orchestrators), not
    # by API clients, so they must not move when the API is versioned.
    app.include_router(health.router, tags=["health"])

    app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(documents.router, prefix="/api/v1/documents", tags=["documents"])
    app.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
    app.include_router(search.router, prefix="/api/v1/search", tags=["search"])
    app.include_router(workspace.router, prefix="/api/v1/workspace", tags=["workspace"])

    return app


app = create_app()
