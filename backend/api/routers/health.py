"""Health check endpoints for monitoring systems."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Basic liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Readiness probe — checks all dependencies."""
    ...  # TODO: check Qdrant, Redis, Claude API connectivity
    return {"status": "ready", "dependencies": {}}


@router.get("/metrics")
async def metrics() -> dict:
    """Prometheus-compatible metrics placeholder."""
    ...  # TODO: integrate prometheus_client
    return {}
