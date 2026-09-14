"""Real PDF bytes for tests, built with PyMuPDF rather than committed as binaries.

Generated, so the repository carries no opaque fixture files and every test
states exactly what its PDF contains. Note that PyMuPDF writes a fresh document
ID and timestamp each time: two calls with the same arguments produce
**different bytes and different hashes**. A test about identical content must
build the bytes once and reuse them.
"""

from typing import Any

import pymupdf

# PyMuPDF ships no type annotations; typed once here as `Any` so the helpers
# below are checked everywhere except the calls into the binding itself.
_pdf: Any = pymupdf


def make_pdf(pages: int = 1, text: str = "ResearchMind test document") -> bytes:
    """A valid, unencrypted PDF with `pages` pages of text."""
    document = _pdf.open()
    for number in range(pages):
        document.new_page().insert_text((72, 72), f"{text} — page {number + 1}")
    data: bytes = document.tobytes()
    document.close()
    return data


def make_encrypted_pdf(
    *, user_password: str = "user-pw", owner_password: str = "owner-pw"
) -> bytes:
    """A PDF that cannot be opened without a password."""
    document = _pdf.open()
    document.new_page().insert_text((72, 72), "locked")
    data: bytes = document.tobytes(
        encryption=_pdf.PDF_ENCRYPT_AES_256,
        owner_pw=owner_password,
        user_pw=user_password,
    )
    document.close()
    return data


def make_owner_restricted_pdf() -> bytes:
    """A PDF with only an owner password — permission flags, readable without one."""
    document = _pdf.open()
    document.new_page().insert_text((72, 72), "restricted but readable")
    data: bytes = document.tobytes(
        encryption=_pdf.PDF_ENCRYPT_AES_256, owner_pw="owner-pw", permissions=0
    )
    document.close()
    return data
