"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.config.settings import get_settings
from backend.db.engine import dispose_engine
from backend.api.routers import documents, agents, search, auth, workspace, health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release infrastructure held by the process on shutdown.

    Startup deliberately does nothing. Connecting here would make a reachable
    database a condition of booting the API, so a momentarily unavailable
    PostgreSQL would turn into a crash-loop instead of failing requests — and
    ``/health`` could no longer answer, which is exactly when a liveness probe
    matters most. The engine connects on first use instead.

    Shutdown disposes the connection pool if one was ever created, so a
    restarting container does not leave sockets open against PostgreSQL.

    Qdrant and Redis clients are not disposed here: both are `@lru_cache`d
    factories with no shutdown hook wired anywhere yet. Adding them is
    Milestone M9's observability work, not this sprint's.
    """
    yield
    await dispose_engine()


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
