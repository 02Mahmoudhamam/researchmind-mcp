"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.config.settings import get_settings
from backend.api.routers import documents, agents, search, auth, workspace, health

settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        docs_url="/docs" if settings.DEBUG else None,
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
