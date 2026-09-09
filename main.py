"""ResearchMind MCP — application entry point."""
import uvicorn
from backend.config.settings import get_settings
from shared.utils.logger import configure_logging

settings = get_settings()

if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        "backend.api.app:app",
        host="0.0.0.0",
        port=settings.APP_PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
