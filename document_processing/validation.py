"""Upload validation — what `docs/security/principles.md` §4 requires before storage.

"Uploads are validated before they are stored — magic-byte MIME sniffing (never
the client `Content-Type`), size cap, page cap, encrypted-PDF rejection."

This module is that sentence, and deliberately nothing more. It opens a PDF only
far enough to answer structural questions — does it open, does it need a
password, how many pages — and never reads a page's content. Text extraction,
section detection and chunking are later M3 sprints, and they run in the
ingestion worker (ADR-0009), not in the request.

No FastAPI, no database, no storage. The REST route, a future MCP tool and the
ingestion worker can all call the same functions and get the same answers.

Every function here is synchronous and some are CPU- or disk-bound — PyMuPDF is
a C extension — so callers on the event loop run them in a worker thread.
"""

import hashlib
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Callable

import pymupdf

# PyMuPDF is a C-extension binding with no type annotations, so under
# `mypy --strict` every call into it is an untyped call. Rather than an ignore
# per call site, the one function this module uses is bound once to a typed
# name: the boundary is visible, it is the only place the library is touched,
# and nothing else in the file loses type checking.
_open_pdf: Callable[..., Any] = pymupdf.open

PDF_MIME_TYPE = "application/pdf"

# The PDF header. Required at offset zero, although PDF readers tolerate up to
# 1024 bytes of leading junk: accepting junk is how a file becomes a polyglot —
# a valid PDF to this check and something else entirely to whatever opens it
# next. Nothing legitimate needs the leniency.
_PDF_MAGIC = b"%PDF-"

# `documents.filename` is VARCHAR(512). Longer names are refused here, with a
# message, rather than reaching PostgreSQL as a 500.
MAX_FILENAME_LENGTH = 512

_READ_CHUNK_BYTES = 1024 * 1024

# Removed from display names: control characters (NUL, newlines, escapes),
# format characters — including the right-to-left override that renders
# "invoice\u202efdp.exe" as "invoiceexe.pdf" — and lone surrogates, which cannot
# be encoded as UTF-8 and would fail at the database.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class RejectionReason(str, Enum):
    """Why an upload was refused. The value is stable; the message is for people."""

    EMPTY = "empty"
    TOO_LARGE = "too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    MALFORMED = "malformed"
    ENCRYPTED = "encrypted"
    TOO_MANY_PAGES = "too_many_pages"
    INVALID_FILENAME = "invalid_filename"


class UploadRejected(Exception):
    """The upload breaks a rule. Nothing has been stored.

    `message` is written to be shown to the person who uploaded the file. It
    never repeats their filename or any of the file's content — both are
    attacker-controlled, and an error message is a place they would be echoed
    into logs and interfaces unescaped.
    """

    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ValidatedPdf:
    """Facts established about an accepted upload.

    Every field is derived from the bytes themselves. None of it comes from the
    request: not the type, which is sniffed; not the size, which is counted; not
    the name of anything.
    """

    content_hash: str
    size_bytes: int
    mime_type: str
    page_count: int


def content_hash(data: bytes) -> str:
    """SHA-256 of the bytes, as lowercase hex — ADR-0008's content address.

    Over the content alone. Not the filename, the owner or the type: the same
    bytes must address the same object wherever and by whomever they arrive.
    """
    return hashlib.sha256(data).hexdigest()


def read_capped(stream: BinaryIO, max_bytes: int) -> bytes:
    """Read a whole upload, refusing it the moment it exceeds `max_bytes`.

    Reads in chunks and stops at the first byte over the limit, so an oversized
    upload is never held in memory in full. (The framework may already have
    spooled it to a temporary file by now — a transport-level limit is
    hardening work, M9 — but this process never loads more than the limit.)

    :raises UploadRejected: TOO_LARGE.
    """
    received = bytearray()
    while True:
        chunk = stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(received)
        received.extend(chunk)
        if len(received) > max_bytes:
            raise UploadRejected(
                RejectionReason.TOO_LARGE,
                f"The file is larger than the maximum upload size of {max_bytes} bytes.",
            )


def display_filename(raw: str | None) -> str:
    """The client's filename, made safe to *store and show*. Never a path.

    ADR-0008 §3 already guarantees the name cannot reach the filesystem — the
    storage key has no place for it. This is about what the name does as
    metadata: shown in a list, returned by the API, quoted in a citation.

    * Only the final path component is kept, splitting on `/` and `\\`, so
      `../../etc/passwd` is stored as `passwd` and `C:\\Users\\x\\paper.pdf`
      as `paper.pdf`. Browsers send bare names; anything else is either an
      unusual client or an attempt.
    * NFC normalisation, so the same visible name is the same stored string. Not
      NFKC: that would rewrite what the user actually named the file.
    * Control, format and surrogate characters are removed.
    * An empty result, `.` or `..`, and names longer than the column, are
      refused rather than invented or truncated.

    :raises UploadRejected: INVALID_FILENAME.
    """
    if raw is None:
        raise UploadRejected(
            RejectionReason.INVALID_FILENAME, "The uploaded file has no filename."
        )

    name = unicodedata.normalize("NFC", raw)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch for ch in name if unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    )
    name = name.strip()

    if not name or name in {".", ".."}:
        raise UploadRejected(
            RejectionReason.INVALID_FILENAME,
            "The uploaded file has no usable filename.",
        )
    if len(name) > MAX_FILENAME_LENGTH:
        raise UploadRejected(
            RejectionReason.INVALID_FILENAME,
            f"The filename is longer than {MAX_FILENAME_LENGTH} characters.",
        )
    return name


def validate_pdf(data: bytes, max_pages: int) -> ValidatedPdf:
    """Decide whether these bytes are a PDF this system will accept.

    In order, each check cheaper than the next: empty; not a PDF by its magic
    bytes; does not open as a PDF; cannot be opened without a password; no
    pages; too many pages.

    "Encrypted" is interpreted as **cannot be read without a password**
    (`needs_pass`). A PDF protected only by an owner password — permission
    flags against printing or copying, which publishers commonly set — opens
    and reads normally, and PyMuPDF reports it as not encrypted. Measured, not
    assumed. Refusing those would refuse ordinary journal articles to enforce a
    restriction the pipeline never relied on.

    Anything PyMuPDF raises while opening is a refusal. A hostile PDF can fail
    in ways no list of exception types anticipates, and the safe answer to all
    of them is the same one.

    :raises UploadRejected: EMPTY, UNSUPPORTED_TYPE, MALFORMED, ENCRYPTED,
        TOO_MANY_PAGES.
    """
    if not data:
        raise UploadRejected(RejectionReason.EMPTY, "The uploaded file is empty.")

    # The client's Content-Type is never consulted. It is a claim; these bytes
    # are the evidence.
    if not data.startswith(_PDF_MAGIC):
        raise UploadRejected(
            RejectionReason.UNSUPPORTED_TYPE, "Only PDF documents are supported."
        )

    try:
        document = _open_pdf(stream=data, filetype="pdf")
    except Exception as exc:
        raise UploadRejected(
            RejectionReason.MALFORMED, "The file could not be read as a PDF."
        ) from exc

    with document:
        if document.needs_pass:
            raise UploadRejected(
                RejectionReason.ENCRYPTED,
                "Password-protected PDFs are not supported.",
            )
        page_count = int(document.page_count)

    if page_count < 1:
        raise UploadRejected(RejectionReason.MALFORMED, "The PDF has no pages.")
    if page_count > max_pages:
        raise UploadRejected(
            RejectionReason.TOO_MANY_PAGES,
            f"The PDF has more than the maximum of {max_pages} pages.",
        )

    return ValidatedPdf(
        content_hash=content_hash(data),
        size_bytes=len(data),
        mime_type=PDF_MIME_TYPE,
        page_count=page_count,
    )
