"""Upload validation: what principles.md §4 requires before anything is stored.

Real PDFs built by PyMuPDF (tests/pdfs.py), real hashing, no mocks. The HTTP
behaviour of the same rules — status codes, nothing stored, nothing recorded —
is tests/integration/test_document_upload.py.
"""

import ast
import hashlib
import inspect
import io
from pathlib import Path

import pytest

from document_processing import validation
from document_processing.validation import (
    MAX_FILENAME_LENGTH,
    PDF_MIME_TYPE,
    RejectionReason,
    UploadRejected,
    content_hash,
    display_filename,
    read_capped,
    validate_pdf,
)
from tests.pdfs import make_encrypted_pdf, make_owner_restricted_pdf, make_pdf


def _reason(call: object) -> RejectionReason:
    """Run a zero-argument callable and return why it refused."""
    with pytest.raises(UploadRejected) as raised:
        call()  # type: ignore[operator]
    return raised.value.reason


class TestContentHash:
    def test_it_is_sha256_hex_of_the_bytes(self) -> None:
        data = make_pdf()

        assert content_hash(data) == hashlib.sha256(data).hexdigest()
        assert len(content_hash(data)) == 64

    def test_the_same_bytes_give_the_same_hash(self) -> None:
        data = make_pdf()

        assert content_hash(data) == content_hash(bytes(data))

    def test_different_bytes_give_different_hashes(self) -> None:
        """Including two PDFs of identical visible content.

        PyMuPDF writes a fresh document id each time, so these differ in bytes —
        and content addressing is about bytes, not about what a reader sees.
        """
        assert content_hash(make_pdf()) != content_hash(make_pdf())
        assert content_hash(b"a") != content_hash(b"b")

    def test_nothing_but_the_bytes_goes_into_it(self) -> None:
        """No filename, owner or type parameter exists to leak in."""
        assert list(inspect.signature(content_hash).parameters) == ["data"]


class TestReadCapped:
    def test_exactly_the_limit_is_accepted(self) -> None:
        assert read_capped(io.BytesIO(b"x" * 100), 100) == b"x" * 100

    def test_one_byte_over_is_refused(self) -> None:
        assert _reason(lambda: read_capped(io.BytesIO(b"x" * 101), 100)) is (
            RejectionReason.TOO_LARGE
        )

    def test_it_stops_reading_once_over_the_limit(self) -> None:
        """An oversized upload is not loaded into memory in full."""
        stream = io.BytesIO(b"x" * (10 * 1024 * 1024))

        with pytest.raises(UploadRejected):
            read_capped(stream, 1024)

        assert stream.tell() <= 2 * 1024 * 1024

    def test_an_empty_stream_reads_as_empty(self) -> None:
        """Emptiness is `validate_pdf`'s refusal, with its own message."""
        assert read_capped(io.BytesIO(b""), 100) == b""

    def test_the_message_states_the_limit(self) -> None:
        with pytest.raises(UploadRejected) as raised:
            read_capped(io.BytesIO(b"x" * 11), 10)

        assert "10 bytes" in raised.value.message


class TestValidatePdf:
    def test_a_valid_pdf_is_described_from_its_own_bytes(self) -> None:
        data = make_pdf(pages=3)

        validated = validate_pdf(data, max_pages=10)

        assert validated.page_count == 3
        assert validated.size_bytes == len(data)
        assert validated.mime_type == PDF_MIME_TYPE
        assert validated.content_hash == hashlib.sha256(data).hexdigest()

    def test_empty_is_refused(self) -> None:
        assert _reason(lambda: validate_pdf(b"", 10)) is RejectionReason.EMPTY

    @pytest.mark.parametrize(
        "data",
        [b"hello world", b"\x89PNG\r\n\x1a\n", b"PK\x03\x04", b"<html>%PDF-1.7"],
        ids=["text", "png", "zip", "html-wrapped"],
    )
    def test_bytes_that_are_not_a_pdf_are_an_unsupported_type(
        self, data: bytes
    ) -> None:
        assert (
            _reason(lambda: validate_pdf(data, 10)) is RejectionReason.UNSUPPORTED_TYPE
        )

    def test_leading_junk_before_the_header_is_refused(self) -> None:
        """Readers tolerate it; this does not. It is how polyglot files are made."""
        data = b"GIF89a" + make_pdf()

        assert (
            _reason(lambda: validate_pdf(data, 10)) is RejectionReason.UNSUPPORTED_TYPE
        )

    @pytest.mark.parametrize(
        "data",
        [b"%PDF-", b"%PDF-1.7\nthis is not a pdf", b"%PDF-1.7\n" + b"\x00" * 4096],
        ids=["header-only", "garbage", "nulls"],
    )
    def test_a_pdf_header_on_garbage_is_malformed(self, data: bytes) -> None:
        """The magic bytes are a claim; opening the file is the evidence."""
        assert _reason(lambda: validate_pdf(data, 10)) is RejectionReason.MALFORMED

    def test_a_password_protected_pdf_is_refused(self) -> None:
        assert _reason(lambda: validate_pdf(make_encrypted_pdf(), 10)) is (
            RejectionReason.ENCRYPTED
        )

    def test_an_owner_restricted_pdf_is_accepted(self) -> None:
        """Permission flags without a user password: readable, and common.

        PyMuPDF opens it without a password and reports it unencrypted —
        measured before this rule was written. Publishers set these routinely;
        refusing them would refuse ordinary papers.
        """
        assert validate_pdf(make_owner_restricted_pdf(), 10).page_count == 1

    def test_exactly_the_page_limit_is_accepted(self) -> None:
        assert validate_pdf(make_pdf(pages=4), max_pages=4).page_count == 4

    def test_one_page_over_is_refused(self) -> None:
        assert _reason(lambda: validate_pdf(make_pdf(pages=5), 4)) is (
            RejectionReason.TOO_MANY_PAGES
        )

    def test_the_client_content_type_cannot_influence_the_result(self) -> None:
        """There is no parameter for it. Sniffing is the only source of type."""
        assert list(inspect.signature(validate_pdf).parameters) == ["data", "max_pages"]


class TestDisplayFilename:
    @pytest.mark.parametrize(
        ("raw", "stored"),
        [
            ("paper.pdf", "paper.pdf"),
            ("My Paper (final) v2.pdf", "My Paper (final) v2.pdf"),
            ("../../etc/passwd", "passwd"),
            ("../../../secret.pdf", "secret.pdf"),
            ("/etc/passwd", "passwd"),
            ("C:\\Users\\alice\\paper.pdf", "paper.pdf"),
            ("..\\..\\windows\\system32\\x.pdf", "x.pdf"),
            ("dir/sub/paper.pdf", "paper.pdf"),
            ("  padded.pdf  ", "padded.pdf"),
            ("naïve café.pdf", "naïve café.pdf"),
            ("研究論文.pdf", "研究論文.pdf"),
            ("cafe\u0301.pdf", "caf\u00e9.pdf"),
            ("line\nbreak.pdf", "linebreak.pdf"),
            ("nul\x00byte.pdf", "nulbyte.pdf"),
            ("invoice\u202efdp.exe", "invoicefdp.exe"),
            ("zero\u200bwidth.pdf", "zerowidth.pdf"),
        ],
        ids=[
            "plain",
            "spaces",
            "traversal",
            "deep-traversal",
            "absolute",
            "windows-absolute",
            "windows-traversal",
            "nested",
            "padded",
            "latin-unicode",
            "cjk",
            "decomposed-accent",
            "newline",
            "nul",
            "rtl-override",
            "zero-width",
        ],
    )
    def test_names_become_safe_display_metadata(self, raw: str, stored: str) -> None:
        assert display_filename(raw) == stored

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", ".", "..", "../", "a/..", "\x00\x01", "\u202e"],
        ids=[
            "none",
            "empty",
            "blank",
            "dot",
            "dotdot",
            "slash",
            "ends-dotdot",
            "controls",
            "bidi-only",
        ],
    )
    def test_a_name_with_nothing_usable_is_refused(self, raw: str | None) -> None:
        assert (
            _reason(lambda: display_filename(raw)) is RejectionReason.INVALID_FILENAME
        )

    def test_a_name_longer_than_the_column_is_refused_not_truncated(self) -> None:
        assert display_filename("a" * MAX_FILENAME_LENGTH) == "a" * MAX_FILENAME_LENGTH
        assert _reason(lambda: display_filename("a" * (MAX_FILENAME_LENGTH + 1))) is (
            RejectionReason.INVALID_FILENAME
        )

    def test_no_refusal_repeats_the_filename(self) -> None:
        """Error messages are shown and logged; the name is attacker-controlled.

        No path separator in it, so it reaches the length check intact and is
        refused there — the case where a naive message would quote it back.
        """
        hostile = "<img src=x onerror=alert(1)>" * 30
        assert len(hostile) > MAX_FILENAME_LENGTH and "/" not in hostile

        with pytest.raises(UploadRejected) as raised:
            display_filename(hostile)

        assert "<img" not in raised.value.message
        assert "onerror" not in raised.value.message


class TestValidationIsNotParsing:
    """S3.1 validates structure. Reading content is the parser's, in a later sprint."""

    FORBIDDEN_CALLS = {
        "get_text",
        "get_textpage",
        "get_text_blocks",
        "get_text_words",
        "load_page",
        "extract_image",
        "get_images",
        "search_for",
    }

    def test_the_module_reads_no_page_content(self) -> None:
        tree = ast.parse(Path(inspect.getfile(validation)).read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert not calls & self.FORBIDDEN_CALLS, calls & self.FORBIDDEN_CALLS

    def test_the_module_does_not_iterate_pages(self) -> None:
        """`for page in document` is text extraction's first line."""
        tree = ast.parse(Path(inspect.getfile(validation)).read_text(encoding="utf-8"))
        loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]

        assert all(
            not (isinstance(loop.iter, ast.Name) and loop.iter.id == "document")
            for loop in loops
        )
