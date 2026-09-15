"""PDF text extraction with PyMuPDF — M3/S3.3 (COMPLETION_PLAN 3.5, ADR-005).

Two layers, deliberately separate:

* `extract_pdf_text(data, max_pages)` — synchronous and self-contained: bytes
  in, `ExtractedText` out. It knows nothing of ARQ, HTTP, the database or where
  the bytes were stored, and is never given a path, a filename, an owner or a
  request. It can be tested, and run, entirely on its own.
* `PyMuPDFTextExtractor` — the async `PdfTextExtractor` the worker uses. It runs
  the function above in a separate process, under a deadline, and kills the
  process if the deadline passes.

**Why a process, when ADR-0009 §3 names `anyio.to_thread.run_sync`.** The ADR's
concern is the event loop, and a thread would protect it. It would not protect
the worker: a Python thread cannot be stopped. A hostile PDF that makes MuPDF
spin would keep a CPU and a thread busy long after its deadline had "fired",
and a few such documents would exhaust the worker. `anyio.to_process.run_sync`
with `cancellable=True` is the same anyio boundary one level stronger: when the
deadline cancels it, the process running MuPDF receives SIGKILL. The bytes go
to the process by pipe; worker processes are pooled and reused.

**Reading order.** ADR-005 requires block mode with column-aware ordering, and
measurement confirmed why: on a two-column page PyMuPDF returns blocks in
content-stream order (whatever order the producer wrote them in), and
`sort=True` interleaves the columns block by block. `reading_order` restores
the order a person reads — see its docstring for the rule and its limits.
"""

import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import anyio
import pymupdf
from anyio import BrokenWorkerProcess, to_process

from shared.interfaces.pdf_extraction import ExtractionFailure, PdfExtractionError
from shared.models.extraction import ExtractedPage, ExtractedText, TextBlock

# PyMuPDF ships no type annotations. As in validation.py, the one function used
# is bound once to a typed name, and the constants are converted to `int` here,
# so nothing below loses type checking.
_open_pdf: Callable[..., Any] = pymupdf.open

# Dict mode, and deliberately *without*:
#   TEXT_PRESERVE_LIGATURES — "ﬁ" becomes "fi", so words are searchable;
#   TEXT_PRESERVE_IMAGES    — image blocks carry no text and are not wanted;
#   TEXT_CID_FOR_UNKNOWN_UNICODE — an unmappable glyph becomes U+FFFD, which is
#       dropped below, instead of an arbitrary code point posing as text.
# TEXT_MEDIABOX_CLIP drops text positioned outside the visible page.
_TEXT_FLAGS: int = int(pymupdf.TEXT_PRESERVE_WHITESPACE) | int(
    pymupdf.TEXT_MEDIABOX_CLIP
)

# Removed from extracted text: control characters (other than whitespace, which
# is collapsed), format characters — including bidirectional overrides, which can
# make text read differently from what it says — surrogates, private-use and
# unassigned code points. None of them is readable text, and NUL in particular
# cannot be stored by PostgreSQL at all.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_REPLACEMENT_CHARACTER = "�"

# Half the gutter test's slack, as a fraction of page width: a block counts as
# crossing the middle of the page only if it extends this far past it on both
# sides. Absorbs the few points by which column edges wander.
_GUTTER_TOLERANCE = 0.02


def clean_text(raw: str) -> str:
    """Extracted text made safe and uniform: NFC, one space between words.

    Unicode is normalised to NFC; every run of whitespace — spaces, tabs,
    no-break spaces, stray line breaks — becomes a single space, and leading and
    trailing whitespace goes; the characters described at `_DROPPED_CATEGORIES`
    are removed. What remains is only what a reader sees.
    """
    text = unicodedata.normalize("NFC", raw)
    kept = "".join(
        ch
        for ch in text
        if ch.isspace()
        or (
            ch != _REPLACEMENT_CHARACTER
            and unicodedata.category(ch) not in _DROPPED_CATEGORIES
        )
    )
    return " ".join(kept.split())


@dataclass(frozen=True)
class LayoutBlock:
    """A block of text with the position `reading_order` sorts by."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    font_size: float


def reading_order(
    blocks: Sequence[LayoutBlock], page_width: float
) -> list[LayoutBlock]:
    """Blocks in the order a person reads a one- or two-column page.

    Walking down the page, a block that **crosses the middle** — a title, an
    abstract, a full-width figure, any single-column paragraph — is read where
    it stands. Between two such blocks lies a band of two-column text: all of
    its left-column blocks are read top to bottom, then all of its right-column
    blocks. A single-column page, whose paragraphs all cross the middle, comes
    out top to bottom.

    Limits, stated rather than hidden: three or more columns are read as two; a
    table whose cells sit either side of the middle is read column by column,
    not row by row. Both are layouts the chunking stage will have to live with,
    and M7's evaluation is where their cost becomes measurable.
    """
    middle = page_width / 2
    slack = page_width * _GUTTER_TOLERANCE
    ordered: list[LayoutBlock] = []
    left: list[LayoutBlock] = []
    right: list[LayoutBlock] = []

    def close_band() -> None:
        ordered.extend(sorted(left, key=lambda b: (b.y0, b.x0)))
        ordered.extend(sorted(right, key=lambda b: (b.y0, b.x0)))
        left.clear()
        right.clear()

    for block in sorted(blocks, key=lambda b: (b.y0, b.x0)):
        if block.x0 < middle - slack and block.x1 > middle + slack:
            close_band()
            ordered.append(block)
        elif block.x1 <= middle + slack:
            left.append(block)
        else:
            right.append(block)
    close_band()
    return ordered


def _dominant_size(sizes: dict[float, int]) -> float:
    """The size most characters are set in; the larger one on a tie."""
    return max(sizes.items(), key=lambda item: (item[1], item[0]))[0]


def _page_blocks(page: Any) -> list[LayoutBlock]:
    raw = page.get_text("dict", flags=_TEXT_FLAGS)
    blocks: list[LayoutBlock] = []
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: dict[float, int] = {}
        for line in block["lines"]:
            spans = line["spans"]
            text = clean_text("".join(str(span["text"]) for span in spans))
            if text:
                lines.append(text)
            for span in spans:
                characters = len(clean_text(str(span["text"])))
                if characters:
                    size = round(float(span["size"]), 1)
                    sizes[size] = sizes.get(size, 0) + characters
        if not lines:
            continue
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        blocks.append(
            LayoutBlock(x0, y0, x1, y1, "\n".join(lines), _dominant_size(sizes))
        )
    return blocks


def extract_pdf_text(data: bytes, max_pages: int) -> ExtractedText:
    """Every page's text, in reading order. Synchronous; call it off the loop.

    In order: open; refuse a PDF that needs a password; refuse one with no pages
    or more than `max_pages` — both before any page is read; then read each page.
    Any exception PyMuPDF raises becomes the matching `ExtractionFailure`, with
    PyMuPDF's own message discarded.

    A PDF with no text on any page is returned, not refused: whether that is a
    failure is the caller's decision, not the parser's.

    :raises PdfExtractionError: OPEN_FAILED, ENCRYPTED, PAGE_LIMIT_EXCEEDED,
        PARSE_FAILED.
    """
    try:
        document = _open_pdf(stream=data, filetype="pdf")
    except Exception:
        raise PdfExtractionError(ExtractionFailure.OPEN_FAILED) from None

    with document:
        if document.needs_pass:
            raise PdfExtractionError(ExtractionFailure.ENCRYPTED)
        page_count = int(document.page_count)
        if page_count < 1:
            raise PdfExtractionError(ExtractionFailure.OPEN_FAILED)
        if page_count > max_pages:
            raise PdfExtractionError(ExtractionFailure.PAGE_LIMIT_EXCEEDED)

        pages: list[ExtractedPage] = []
        try:
            for index in range(page_count):
                page = document.load_page(index)
                ordered = reading_order(_page_blocks(page), float(page.rect.width))
                pages.append(
                    ExtractedPage(
                        page_number=index + 1,
                        blocks=tuple(
                            TextBlock(text=block.text, font_size=block.font_size)
                            for block in ordered
                        ),
                    )
                )
        except Exception:
            raise PdfExtractionError(ExtractionFailure.PARSE_FAILED) from None

    return ExtractedText(page_count=page_count, pages=tuple(pages))


class PyMuPDFTextExtractor:
    """`PdfTextExtractor` running `extract_pdf_text` in a killable process."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_concurrency: int,
        extract: Callable[[bytes, int], ExtractedText] = extract_pdf_text,
    ) -> None:
        """
        :param timeout_seconds: the most one extraction may take. Past it, the
            process is killed and TIMEOUT raised.
        :param max_concurrency: extractions that may run at once. The worker
            passes its own job concurrency, so no job waits for a process — and
            waiting never eats into a document's time budget.
        :param extract: what runs in the process. Must be importable by module
            path, because it is pickled across. Replaceable for tests.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._timeout = timeout_seconds
        self._limiter = anyio.CapacityLimiter(max_concurrency)
        self._extract = extract

    async def extract(self, data: bytes, *, max_pages: int) -> ExtractedText:
        """Extract in a worker process; kill it if the time budget runs out.

        A cancellation from outside — the job's own timeout, or the worker
        shutting down — also kills the process, and propagates unchanged.

        :raises PdfExtractionError: TIMEOUT, PARSER_UNAVAILABLE, or whatever
            `extract_pdf_text` raised.
        """
        try:
            with anyio.fail_after(self._timeout):
                return await to_process.run_sync(
                    self._extract,
                    data,
                    max_pages,
                    cancellable=True,
                    limiter=self._limiter,
                )
        except TimeoutError:
            raise PdfExtractionError(ExtractionFailure.TIMEOUT) from None
        except BrokenWorkerProcess:
            raise PdfExtractionError(ExtractionFailure.PARSER_UNAVAILABLE) from None
