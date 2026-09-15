"""Extracting text from a PDF — the seam between ingestion and the parser.

`IngestionService` depends on `PdfTextExtractor`, never on PyMuPDF: the service
decides what a result means for a document, the extractor only reads bytes. An
extractor receives bytes the worker has already verified against their SHA-256,
and nothing else — no path, no filename, no owner, no request, no `Principal`.
It cannot reach storage, the database or an identity, because it is never given
a way to.
"""

from enum import Enum
from typing import Protocol

from shared.models.extraction import ExtractedText


class ExtractionFailure(str, Enum):
    """Why text could not be extracted. Stable codes, safe to persist and show.

    The values double as ingestion failure reasons, so none carries a path, an
    exception message or any of the document's content.
    """

    # PyMuPDF could not open the bytes as a PDF, or the PDF has no pages.
    OPEN_FAILED = "pdf_open_failed"
    # The PDF cannot be read without a password.
    ENCRYPTED = "pdf_encrypted"
    # More pages than the configured maximum. Checked before any page is read.
    PAGE_LIMIT_EXCEEDED = "page_limit_exceeded"
    # The PDF opened, but reading a page's text failed.
    PARSE_FAILED = "pdf_parse_failed"
    # Extraction ran past its time budget and was stopped.
    TIMEOUT = "pdf_timeout"
    # The extraction process died or could not be started. The only failure that
    # may not recur: nothing about the document is implicated.
    PARSER_UNAVAILABLE = "parser_unavailable"


class PdfExtractionError(Exception):
    """Extraction failed for a reason in `ExtractionFailure`.

    Constructed from the reason alone, and its message is only the reason's
    code: it crosses a process boundary by pickling, and must never carry
    PyMuPDF's own message — which can quote the document — into a log.
    """

    def __init__(self, reason: ExtractionFailure | str) -> None:
        self.reason = ExtractionFailure(reason)
        super().__init__(self.reason.value)

    @property
    def transient(self) -> bool:
        """Whether retrying could succeed. Only when the parser, not the PDF, failed."""
        return self.reason is ExtractionFailure.PARSER_UNAVAILABLE


class PdfTextExtractor(Protocol):
    """Turns verified PDF bytes into `ExtractedText`."""

    async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
        """Extract every page's text, in reading order.

        Must not block the event loop, and must return or raise within the
        implementation's time budget however hostile the PDF is.

        :param max_pages: refuse a PDF with more pages, before reading any.
        :raises PdfExtractionError: for every failure, with its reason.
        """
        ...
