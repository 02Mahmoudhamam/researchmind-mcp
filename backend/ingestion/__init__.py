"""Ingestion queue implementations. The contract is `shared/interfaces/ingestion.py`."""

from backend.ingestion.queue import DeferredIngestionQueue

__all__ = ["DeferredIngestionQueue"]
