"""Finding a paper's sections in the blocks S3.3 stored — M3/S3.4.

ADR-005 §2 asks for headings "by regex + font-size heuristics", and those are
the signals available: S3.3 persisted each block's text, its dominant font size
and its page (ADR-0011). Position and whitespace were never stored, so the
remaining signals ADR-005 lists cannot be used without changing that format for
a heuristic nothing has measured yet (ADR-0012 §3).

Deterministic, and pure: blocks in, sections out. No model, no LLM, no clock.

A section runs from its heading to the next heading, and **includes** the
heading block — nothing is dropped, and the heading is usually the best short
description of what follows. Text before the first heading is a section with no
title rather than one invented for it.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from shared.models.extraction import ExtractedPage

# A heading is short. Twelve words admits "3.2 Related Work on Column Ordering"
# and excludes any sentence of prose.
MAX_HEADING_WORDS = 12

# Visibly larger type: 15% above the body size. Below that lies the ordinary
# variation between a paragraph and its emphasis.
HEADING_SIZE_RATIO = 1.15

# "1", "2.3", "4.1.2", optionally followed by a dot, then a title.
_NUMBERED = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
# "IV." or "VII " — roman numerals, as older papers number sections.
_ROMAN = re.compile(r"^[IVXLCDM]{1,7}\.?\s+\S")

# The sections a paper actually has, per ADR-005 §2, with the variants that
# appear in practice. Matched case-insensitively, optionally numbered, and
# optionally followed by a colon.
_KNOWN_SECTIONS = (
    r"abstract|introduction|background|related\s+work|prior\s+work|"
    r"method(?:s|ology)?|materials(?:\s+and\s+methods)?|approach|"
    r"experiments?|experimental\s+setup|evaluation|results?|analysis|"
    r"discussion|limitations|threats\s+to\s+validity|"
    r"conclusions?(?:\s+and\s+future\s+work)?|future\s+work|"
    r"acknowledg(?:e)?ments?|references|bibliography|works\s+cited|"
    r"literature\s+cited|appendix(?:\s+[A-Z])?|supplementary(?:\s+material)?"
)
_KNOWN = re.compile(
    rf"^(?:\d+(?:\.\d+)*\.?\s+|[IVXLCDM]{{1,7}}\.?\s+)?(?:{_KNOWN_SECTIONS})\s*:?\s*$",
    re.IGNORECASE,
)

# The heading a references section carries, however it is numbered.
_REFERENCES = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+|[IVXLCDM]{1,7}\.?\s+)?"
    r"(?:references|bibliography|works\s+cited|literature\s+cited)\s*:?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SectionBlock:
    """One block of a section's text, and the page it was read from."""

    text: str
    page_number: int


@dataclass(frozen=True)
class Section:
    """A heading and the blocks beneath it, in reading order."""

    title: str | None
    blocks: tuple[SectionBlock, ...]

    @property
    def is_references(self) -> bool:
        """Whether this section is a reference list (ADR-005 §4)."""
        return self.title is not None and bool(_REFERENCES.match(self.title.strip()))


def body_font_size(pages: Sequence[ExtractedPage]) -> float:
    """The size most of the document's characters are set in.

    Weighted by characters, not by block count: a paper has one title and
    hundreds of paragraph lines, and it is the paragraphs that define "body".
    Ties go to the smaller size, which is the more conservative body estimate —
    it makes the heading test harder to pass, not easier.
    """
    weights: dict[float, int] = {}
    for page in pages:
        for block in page.blocks:
            weights[block.font_size] = weights.get(block.font_size, 0) + len(block.text)
    if not weights:
        return 0.0
    return max(weights.items(), key=lambda item: (item[1], -item[0]))[0]


def is_heading(text: str, font_size: float, *, body_size: float) -> bool:
    """Whether this block is a section heading (ADR-0012 §3).

    Short, and then any of: numbered like a section, named like one, or set in
    visibly larger type than the body. A block of prose is none of those,
    however it is typeset.
    """
    stripped = text.strip()
    if not stripped or "\n" in stripped:
        # More than one line of text is a paragraph, not a heading.
        return False
    if len(stripped.split()) > MAX_HEADING_WORDS:
        return False
    if _KNOWN.match(stripped):
        return True
    if _NUMBERED.match(stripped) or _ROMAN.match(stripped):
        return True
    if body_size > 0 and font_size >= body_size * HEADING_SIZE_RATIO:
        # Unicode scripts without spaces would fail the word count, so guard
        # against a whole paragraph in large type being called a heading.
        return len(stripped) <= 80 and not _ends_a_sentence(stripped)
    return False


def _ends_a_sentence(text: str) -> bool:
    """Headings do not end in sentence punctuation; sentences do."""
    return text[-1] in ".。!?！？" and not _NUMBERED.match(text)


def detect_sections(pages: Sequence[ExtractedPage]) -> tuple[Section, ...]:
    """Split a document's blocks into sections, in reading order.

    Every block ends up in exactly one section, so no text is lost. A document
    with no detectable heading comes back as one untitled section, which is
    what makes the chunker's fixed-window fallback the ordinary path rather
    than a special case.
    """
    body_size = body_font_size(pages)
    sections: list[Section] = []
    title: str | None = None
    blocks: list[SectionBlock] = []

    def close() -> None:
        if blocks:
            sections.append(Section(title=title, blocks=tuple(blocks)))
        blocks.clear()

    for page in pages:
        for block in page.blocks:
            text = _normalise_heading_spacing(block.text)
            if is_heading(text, block.font_size, body_size=body_size):
                close()
                title = text.strip()
            blocks.append(SectionBlock(text=block.text, page_number=page.page_number))
    close()
    return tuple(sections)


def _normalise_heading_spacing(text: str) -> str:
    """NFC and single spaces, for matching only — the block's text is untouched.

    S3.3 already normalised what it stored; this guards the patterns against a
    heading whose lines were joined, without ever rewriting stored text.
    """
    return " ".join(unicodedata.normalize("NFC", text).split())
