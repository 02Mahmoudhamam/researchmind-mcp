"""Ingestion queue and worker. The contract is `shared/interfaces/ingestion.py`."""

from backend.ingestion.queue import ArqIngestionQueue

__all__ = ["ArqIngestionQueue"]
