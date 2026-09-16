"""Splitting a parsed document into chunks — the seam ingestion depends on.

`IngestionService` decides what a chunking result means for a document; the
chunker decides where the boundaries are. Like the extractor seam, a chunker is
handed only what it needs — the pages the worker read, owner-scoped, from the
database — and has no path to storage, the queue, an identity or a request.

An implementation must be deterministic: the same pages must always produce the
same chunks, in the same order, with the same content (ADR-0012 §8).
"""

from typing import Protocol, Sequence

from shared.models.chunking import ChunkingResult
from shared.models.extraction import ExtractedPage


class DocumentChunker(Protocol):
    """Turns a document's extracted pages into chunks, with their provenance."""

    def chunk(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
        """Every chunk of the document, in reading order, indexed from zero.

        Returns an empty result when the pages hold no text: whether that is a
        failure is the caller's decision, not the chunker's.
        """
        ...
