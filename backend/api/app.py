"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.config.settings import get_settings
from backend.db.engine import dispose_engine
from backend.api.routers import documents, agents, search, auth, workspace, health

settings = get_settings()


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
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        docs_url="/docs" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
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
