"""PDF text extraction — M3/S3.3. No database, no Redis, no mocks of PyMuPDF.

Real PDFs are built by tests/pdfs.py; the extractor's process boundary is real
too, with the functions it runs in the child defined in tests/extraction_helpers.py
(they must be importable by module path to cross it).

How ingestion *uses* extraction — statuses, persisted pages, failure reasons —
is tests/integration/test_pdf_ingestion.py.
"""

import asyncio
import inspect
import os
import pickle
import time
import traceback
from pathlib import Path
from typing import Any

import anyio
import pydantic
import pymupdf
import pytest

from document_processing import pdf_parser
from document_processing.pdf_parser import (
    LayoutBlock,
    PyMuPDFTextExtractor,
    clean_text,
    extract_pdf_text,
    reading_order,
)
from shared.interfaces.pdf_extraction import (
    ExtractionFailure,
    PdfExtractionError,
    PdfTextExtractor,
)
from shared.models.extraction import ExtractedPage, ExtractedText, TextBlock
from shared.models.ingestion import IngestionReason
from tests import extraction_helpers
from tests.pdfs import (
    make_encrypted_pdf,
    make_heading_pdf,
    make_image_only_pdf,
    make_owner_restricted_pdf,
    make_pages_pdf,
    make_pdf,
    make_two_column_pdf,
    make_unicode_pdf,
)


def _reason(data: bytes, max_pages: int = 50) -> ExtractionFailure:
    with pytest.raises(PdfExtractionError) as raised:
        extract_pdf_text(data, max_pages)
    return raised.value.reason


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; it is dead for every purpose here.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


# =============================================================================
# Cleaning
# =============================================================================


class TestCleanText:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("plain words", "plain words"),
            ("  padded  ", "padded"),
            ("tabs\tand\t\tspaces   between", "tabs and spaces between"),
            ("no\u00a0break\u2003em space", "no break em space"),
            ("line\nbreak\r\nkept apart", "line break kept apart"),
            ("cafe\u0301", "caf\u00e9"),
            ("研究論文", "研究論文"),
            ("مرحبا بالعالم", "مرحبا بالعالم"),
        ],
        ids=[
            "plain",
            "padded",
            "tabs",
            "unicode-spaces",
            "newlines",
            "nfc",
            "cjk",
            "arabic",
        ],
    )
    def test_text_is_normalised_and_whitespace_collapsed(
        self, raw: str, clean: str
    ) -> None:
        assert clean_text(raw) == clean

    @pytest.mark.parametrize(
        "unreadable",
        [
            "\x00",  # NUL — PostgreSQL cannot store it at all
            "\x07",  # a control character
            "\u202e",  # right-to-left override: text that reads unlike it is
            "\u200b",  # zero-width space
            "\ue000",  # private use
            "\ufffd",  # an unmappable glyph
        ],
        ids=["nul", "bell", "bidi-override", "zero-width", "private-use", "fffd"],
    )
    def test_characters_nobody_can_read_are_removed(self, unreadable: str) -> None:
        assert clean_text(f"safe{unreadable}text") == "safetext"

    def test_nothing_readable_leaves_nothing(self) -> None:
        assert clean_text("\x00\u200b \t\ufffd") == ""


# =============================================================================
# Reading order
# =============================================================================


def _block(x0: float, y0: float, x1: float, label: str) -> LayoutBlock:
    return LayoutBlock(x0, y0, x1, y0 + 20, label, 10.0)


class TestReadingOrder:
    WIDTH = 600.0

    def _labels(self, blocks: list[LayoutBlock]) -> list[str]:
        return [b.text for b in reading_order(blocks, self.WIDTH)]

    def test_a_single_column_reads_top_to_bottom(self) -> None:
        blocks = [
            _block(60, 300, 540, "third"),
            _block(60, 100, 540, "first"),
            _block(60, 200, 540, "second"),
        ]

        assert self._labels(blocks) == ["first", "second", "third"]

    def test_two_columns_read_left_column_then_right(self) -> None:
        blocks = [
            _block(320, 100, 560, "R1"),
            _block(320, 300, 560, "R2"),
            _block(40, 100, 280, "L1"),
            _block(40, 300, 280, "L2"),
        ]

        assert self._labels(blocks) == ["L1", "L2", "R1", "R2"]

    def test_full_width_blocks_stand_between_column_bands(self) -> None:
        """A figure across both columns closes one band and opens the next."""
        blocks = [
            _block(40, 20, 560, "title"),
            _block(320, 100, 560, "R1"),
            _block(40, 100, 280, "L1"),
            _block(40, 400, 560, "figure"),
            _block(320, 500, 560, "R2"),
            _block(40, 500, 280, "L2"),
            _block(150, 750, 450, "footer"),
        ]

        assert self._labels(blocks) == [
            "title",
            "L1",
            "R1",
            "figure",
            "L2",
            "R2",
            "footer",
        ]

    def test_a_centred_heading_is_read_where_it_stands(self) -> None:
        """Narrow, but across the middle: not assigned to either column."""
        blocks = [
            _block(40, 200, 280, "L"),
            _block(250, 150, 350, "heading"),
            _block(320, 200, 560, "R"),
        ]

        assert self._labels(blocks) == ["heading", "L", "R"]

    def test_the_gutter_tolerates_column_edges_that_wander(self) -> None:
        """A left column ending a few points past the middle is still the left."""
        blocks = [
            _block(320, 100, 560, "R"),
            _block(40, 120, 305, "L"),
        ]

        assert self._labels(blocks) == ["L", "R"]

    def test_nothing_in_nothing_out(self) -> None:
        assert reading_order([], self.WIDTH) == []


# =============================================================================
# extract_pdf_text — the synchronous extractor, on real PDFs
# =============================================================================


class TestExtraction:
    def test_every_page_is_numbered_and_keeps_its_own_text(self) -> None:
        data = make_pdf(pages=4, text="Marker")

        extracted = extract_pdf_text(data, 10)

        assert extracted.page_count == 4
        assert [page.page_number for page in extracted.pages] == [1, 2, 3, 4]
        for page in extracted.pages:
            # make_pdf writes "Marker — page N"; its built-in font has no glyph
            # for the dash, so only the words around it are asserted.
            assert page.text.startswith("Marker ")
            assert page.text.endswith(f" page {page.page_number}")

    def test_a_paragraph_is_one_block_with_its_lines(self) -> None:
        extracted = extract_pdf_text(make_heading_pdf(), 10)

        heading, paragraph = extracted.pages[0].blocks
        assert heading.text == "1 Introduction"
        assert paragraph.text.startswith("Prose that wraps")
        assert len(paragraph.text.splitlines()) > 1, "lines kept within the block"

    def test_a_block_carries_its_dominant_font_size(self) -> None:
        """What ADR-005's heading detection needs, so chunking need not re-parse."""
        heading, paragraph = extract_pdf_text(make_heading_pdf(), 10).pages[0].blocks

        assert (heading.font_size, paragraph.font_size) == (18.0, 10.0)

    def test_two_columns_come_out_in_reading_order(self) -> None:
        """Written right column first; read left column first."""
        blocks = extract_pdf_text(make_two_column_pdf(), 10).pages[0].blocks

        assert [block.text.split()[0] for block in blocks] == [
            "TITLE",
            "ABSTRACT",
            "LEFT-A",
            "LEFT-B",
            "RIGHT-A",
            "RIGHT-B",
            "FOOTER",
        ]

    def test_pymupdf_on_its_own_would_get_that_page_wrong(self) -> None:
        """Why reading_order exists: both PyMuPDF orders are measured wrong."""
        library: Any = pymupdf
        document = library.open(stream=make_two_column_pdf(), filetype="pdf")
        with document:
            page = document[0]
            stream_order = [b[4].split()[0] for b in page.get_text("blocks")]
            sorted_order = [b[4].split()[0] for b in page.get_text("blocks", sort=True)]

        expected = ["TITLE", "ABSTRACT", "LEFT-A", "LEFT-B"]
        assert stream_order[2:4] != expected[2:4]
        assert sorted_order[2:4] != expected[2:4]

    def test_unicode_survives(self) -> None:
        blocks = extract_pdf_text(make_unicode_pdf(), 10).pages[0].blocks

        assert [block.text for block in blocks] == [
            "Café naïve résumé, Zürich",
            "研究論文の要旨",
        ]

    def test_runs_of_whitespace_become_one_space(self) -> None:
        data = make_pages_pdf(["spaced     out    words"])

        assert extract_pdf_text(data, 10).pages[0].text == "spaced out words"

    def test_an_empty_page_keeps_its_place(self) -> None:
        """Dropping it would renumber every page after it."""
        extracted = extract_pdf_text(make_pages_pdf(["one", None, "three"]), 10)

        assert [(p.page_number, p.text) for p in extracted.pages] == [
            (1, "one"),
            (2, ""),
            (3, "three"),
        ]
        assert extracted.pages[1].blocks == ()
        assert extracted.has_text

    def test_an_image_only_pdf_is_returned_empty_not_refused(self) -> None:
        """Whether no text is a failure is the ingestion service's call."""
        extracted = extract_pdf_text(make_image_only_pdf(pages=2), 10)

        assert extracted.page_count == 2
        assert all(page.blocks == () for page in extracted.pages)
        assert not extracted.has_text

    def test_an_owner_restricted_pdf_is_read(self) -> None:
        """Permission flags without a user password: readable, as at upload."""
        assert extract_pdf_text(make_owner_restricted_pdf(), 10).pages[0].text == (
            "restricted but readable"
        )

    def test_ligatures_are_expanded_not_preserved(self) -> None:
        assert not pdf_parser._TEXT_FLAGS & int(pymupdf.TEXT_PRESERVE_LIGATURES)
        assert not pdf_parser._TEXT_FLAGS & int(pymupdf.TEXT_PRESERVE_IMAGES)


class TestExtractionRefuses:
    @pytest.mark.parametrize(
        "data",
        [b"", b"not a pdf at all", b"%PDF-1.7\nthis is not a pdf", b"%PDF-"],
        ids=["empty", "not-pdf", "garbage-after-header", "header-only"],
    )
    def test_bytes_that_do_not_open_as_a_pdf(self, data: bytes) -> None:
        assert _reason(data) is ExtractionFailure.OPEN_FAILED

    def test_pymupdfs_own_error_is_not_chained_onto_the_refusal(self) -> None:
        with pytest.raises(PdfExtractionError) as raised:
            extract_pdf_text(b"%PDF-1.7\nthis is not a pdf", 10)

        rendered = "".join(traceback.format_exception(raised.value))
        assert "During handling" not in rendered
        assert "direct cause" not in rendered

    def test_a_password_protected_pdf(self) -> None:
        assert _reason(make_encrypted_pdf()) is ExtractionFailure.ENCRYPTED

    def test_exactly_the_page_limit_is_read(self) -> None:
        assert extract_pdf_text(make_pdf(pages=5), max_pages=5).page_count == 5

    def test_one_page_over_the_limit_is_refused_before_any_page_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def must_not_read(page: Any) -> Any:
            raise AssertionError("a page was read from an over-limit PDF")

        monkeypatch.setattr(pdf_parser, "_page_blocks", must_not_read)

        assert _reason(make_pdf(pages=6), max_pages=5) is (
            ExtractionFailure.PAGE_LIMIT_EXCEEDED
        )

    def test_a_page_that_cannot_be_read_is_a_parse_failure_without_its_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(page: Any) -> Any:
            raise RuntimeError("SECRET text from inside the document")

        monkeypatch.setattr(pdf_parser, "_page_blocks", broken)

        with pytest.raises(PdfExtractionError) as raised:
            extract_pdf_text(make_pdf(), 10)

        assert raised.value.reason is ExtractionFailure.PARSE_FAILED
        assert str(raised.value) == "pdf_parse_failed"
        assert "SECRET" not in repr(raised.value)
        # Not chained, explicitly or implicitly: a log line with a traceback
        # would otherwise print PyMuPDF's exception — and the document with it.
        rendered = "".join(traceback.format_exception(raised.value))
        assert "SECRET" not in rendered
        assert "RuntimeError" not in rendered


# =============================================================================
# The models and the error
# =============================================================================


class TestTheOutputModel:
    def test_pages_must_be_numbered_one_to_page_count(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ExtractedText(
                page_count=2,
                pages=(ExtractedPage(page_number=1), ExtractedPage(page_number=3)),
            )
        with pytest.raises(pydantic.ValidationError):
            ExtractedText(page_count=2, pages=(ExtractedPage(page_number=1),))

    def test_a_block_cannot_be_blank(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            TextBlock(text="  \n ", font_size=10)

    def test_it_is_frozen(self) -> None:
        page = ExtractedPage(page_number=1)
        with pytest.raises(pydantic.ValidationError):
            page.page_number = 2


class TestTheError:
    def test_it_survives_the_process_boundary(self) -> None:
        error = pickle.loads(
            pickle.dumps(PdfExtractionError(ExtractionFailure.ENCRYPTED))
        )

        assert error.reason is ExtractionFailure.ENCRYPTED
        assert str(error) == "pdf_encrypted"

    def test_only_an_unavailable_parser_is_worth_retrying(self) -> None:
        transient = {r for r in ExtractionFailure if PdfExtractionError(r).transient}

        assert transient == {ExtractionFailure.PARSER_UNAVAILABLE}

    def test_every_failure_is_also_an_ingestion_reason(self) -> None:
        """The service persists `exc.reason.value` as the document's reason."""
        for failure in ExtractionFailure:
            assert IngestionReason(failure.value).value == failure.value


# =============================================================================
# PyMuPDFTextExtractor — the process boundary and the deadline
# =============================================================================


class TestTheProcessExtractor:
    async def test_it_extracts_exactly_what_the_function_does(self) -> None:
        data = make_two_column_pdf()
        extractor = PyMuPDFTextExtractor(timeout_seconds=30, max_concurrency=2)

        assert await extractor.extract(data, max_pages=10) == extract_pdf_text(data, 10)

    async def test_it_runs_in_another_process(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "pid"
        extractor = PyMuPDFTextExtractor(
            timeout_seconds=30,
            max_concurrency=1,
            extract=extraction_helpers.report_pid,
        )

        await extractor.extract(str(pid_file).encode(), max_pages=1)

        assert int(pid_file.read_text()) != os.getpid()

    async def test_failures_raised_in_the_process_arrive_with_their_reason(
        self,
    ) -> None:
        extractor = PyMuPDFTextExtractor(timeout_seconds=30, max_concurrency=1)

        with pytest.raises(PdfExtractionError) as raised:
            await extractor.extract(make_encrypted_pdf(), max_pages=10)

        assert raised.value.reason is ExtractionFailure.ENCRYPTED

    async def test_past_its_deadline_the_process_is_killed(
        self, tmp_path: Path
    ) -> None:
        """A PDF that makes MuPDF spin cannot hold the worker: it is stopped."""
        pid_file = tmp_path / "pid"
        extractor = PyMuPDFTextExtractor(
            timeout_seconds=1,
            max_concurrency=1,
            extract=extraction_helpers.record_pid_then_sleep,
        )
        started = time.monotonic()

        with anyio.fail_after(20):  # if the deadline were bypassed: fail, not hang
            with pytest.raises(PdfExtractionError) as raised:
                await extractor.extract(str(pid_file).encode(), max_pages=60)

        assert raised.value.reason is ExtractionFailure.TIMEOUT
        assert time.monotonic() - started < 10
        pid = int(pid_file.read_text())
        with anyio.fail_after(5):
            while _alive(pid):
                await anyio.sleep(0.05)

    async def test_the_event_loop_keeps_running_while_a_pdf_is_parsed(
        self, tmp_path: Path
    ) -> None:
        extractor = PyMuPDFTextExtractor(
            timeout_seconds=2,
            max_concurrency=1,
            extract=extraction_helpers.record_pid_then_sleep,
        )
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        ticker = asyncio.ensure_future(tick())
        try:
            with pytest.raises(PdfExtractionError):
                await extractor.extract(str(tmp_path / "pid").encode(), max_pages=60)
        finally:
            ticker.cancel()

        assert ticks >= 10, "the loop was blocked during extraction"

    async def test_a_cancelled_extraction_kills_the_process_and_stays_cancelled(
        self, tmp_path: Path
    ) -> None:
        """The job timing out, or the worker stopping: not reported as pdf_timeout."""
        pid_file = tmp_path / "pid"
        extractor = PyMuPDFTextExtractor(
            timeout_seconds=60,
            max_concurrency=1,
            extract=extraction_helpers.record_pid_then_sleep,
        )
        task = asyncio.ensure_future(
            extractor.extract(str(pid_file).encode(), max_pages=60)
        )
        with anyio.fail_after(20):
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        pid = int(pid_file.read_text())
        with anyio.fail_after(5):
            while _alive(pid):
                await anyio.sleep(0.05)

    async def test_a_process_that_dies_is_an_unavailable_parser(self) -> None:
        extractor = PyMuPDFTextExtractor(
            timeout_seconds=30, max_concurrency=1, extract=extraction_helpers.die
        )

        with pytest.raises(PdfExtractionError) as raised:
            await extractor.extract(b"", max_pages=1)

        assert raised.value.reason is ExtractionFailure.PARSER_UNAVAILABLE
        assert raised.value.transient

    @pytest.mark.parametrize(
        ("timeout", "concurrency"), [(0, 1), (-1, 1), (5, 0)], ids=["zero", "neg", "c0"]
    )
    def test_a_budget_or_concurrency_that_permits_nothing_is_refused(
        self, timeout: float, concurrency: int
    ) -> None:
        with pytest.raises(ValueError):
            PyMuPDFTextExtractor(timeout_seconds=timeout, max_concurrency=concurrency)


class TestTheSeam:
    """What a parser is handed is what bounds what it can reach."""

    def test_the_protocol_takes_bytes_and_a_page_limit_only(self) -> None:
        parameters = list(inspect.signature(PdfTextExtractor.extract).parameters)

        assert parameters == ["self", "data", "max_pages"]

    def test_the_implementation_matches_it_exactly(self) -> None:
        for function in (PyMuPDFTextExtractor.extract, extract_pdf_text):
            names = [
                name
                for name in inspect.signature(function).parameters
                if name != "self"
            ]
            assert names == ["data", "max_pages"], function
