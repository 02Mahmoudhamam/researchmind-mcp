"""Structured logging configuration for production."""
import logging
import structlog


def setup_logging(log_level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper()))
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
