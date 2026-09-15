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


# --- M3/S3.3: PDFs whose text layout a test depends on -----------------------

_PARAGRAPH = (
    "Prose that wraps across several lines, so that each column holds a real "
    "paragraph rather than a single line, as the columns of a paper do. "
) * 3


def make_two_column_pdf() -> bytes:
    """A title and abstract across the page, two columns, and a footer.

    The right column is written into the content stream *before* the left, so
    a parser that follows stream order reads the columns backwards, and one
    that sorts blocks by position interleaves them. Reading order is: TITLE,
    ABSTRACT, LEFT-A, LEFT-B, RIGHT-A, RIGHT-B, FOOTER.
    """
    document = _pdf.open()
    page = document.new_page(width=612, height=792)
    rect = _pdf.Rect
    page.insert_textbox(
        rect(50, 40, 562, 90), "TITLE of the paper", fontsize=18, align=1
    )
    page.insert_textbox(
        rect(50, 95, 562, 140), "ABSTRACT " + _PARAGRAPH[:180], fontsize=9
    )
    page.insert_textbox(rect(316, 150, 562, 400), "RIGHT-A " + _PARAGRAPH, fontsize=10)
    page.insert_textbox(rect(316, 420, 562, 700), "RIGHT-B " + _PARAGRAPH, fontsize=10)
    page.insert_textbox(rect(50, 150, 296, 400), "LEFT-A " + _PARAGRAPH, fontsize=10)
    page.insert_textbox(rect(50, 420, 296, 700), "LEFT-B " + _PARAGRAPH, fontsize=10)
    page.insert_textbox(
        rect(50, 720, 562, 760), "FOOTER " + _PARAGRAPH[:90], fontsize=8
    )
    data: bytes = document.tobytes()
    document.close()
    return data


def make_pages_pdf(texts: list[str | None], *, fontsize: float = 11) -> bytes:
    """One page per entry: that text at the top, or a blank page for None."""
    document = _pdf.open()
    for text in texts:
        page = document.new_page()
        if text is not None:
            page.insert_textbox(_pdf.Rect(72, 72, 523, 770), text, fontsize=fontsize)
    data: bytes = document.tobytes()
    document.close()
    return data


def make_heading_pdf() -> bytes:
    """An 18pt heading above an 10pt paragraph — what heading detection keys on."""
    document = _pdf.open()
    page = document.new_page()
    page.insert_textbox(_pdf.Rect(72, 60, 523, 100), "1 Introduction", fontsize=18)
    page.insert_textbox(_pdf.Rect(72, 120, 523, 400), _PARAGRAPH, fontsize=10)
    data: bytes = document.tobytes()
    document.close()
    return data


def make_unicode_pdf() -> bytes:
    """Latin accents with a Latin font, and CJK with PyMuPDF's built-in CJK font."""
    document = _pdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Café naïve résumé, Zürich", fontsize=11)
    page.insert_text((72, 100), "研究論文の要旨", fontname="china-s", fontsize=11)
    data: bytes = document.tobytes()
    document.close()
    return data


def make_image_only_pdf(pages: int = 1) -> bytes:
    """A PDF of pictures and no text — what a scanned paper looks like to a parser."""
    document = _pdf.open()
    for _ in range(pages):
        page = document.new_page()
        pixmap = _pdf.Pixmap(_pdf.csRGB, _pdf.IRect(0, 0, 64, 64), False)
        pixmap.clear_with(180)
        page.insert_image(_pdf.Rect(72, 72, 400, 400), pixmap=pixmap)
    data: bytes = document.tobytes()
    document.close()
    return data
