"""Section-aware chunking — M3/S3.4 (COMPLETION_PLAN 3.6, ADR-005, ADR-0012).

Pages in, chunks out. Pure and deterministic: no database, no queue, no clock,
no randomness. The same pages, configuration, tokenizer and strategy produce the
same chunks, in the same order, byte for byte.

The shape, from ADR-005 §3 and §4:

* Chunks are token windows **inside one section** — never across two, so a
  chunk is never half one argument and half the next.
* A window is `chunk_size` tokens and steps `chunk_size - overlap`, so
  consecutive chunks of a section share their edges.
* A **references** section is split on entry boundaries instead, packing whole
  entries up to the budget and never cutting one in half. Reference chunks
  carry no overlap: repeating whole citations into the next chunk buys nothing.
* A section whose entries cannot be found, or a document with no headings at
  all, falls back to plain windows — a degradation, never a silence.

A chunk's `content` is a **verbatim substring** of the section's text, which is
itself the block text S3.3 stored, joined by blank lines. Chunking normalises
nothing: what a citation quotes is what was extracted (ADR-0012 §2).
"""

import re
from dataclasses import dataclass
from typing import Sequence

from document_processing.sections import Section, detect_sections
from shared.interfaces.tokenization import Tokenizer
from shared.models.chunking import Chunk, ChunkingResult
from shared.models.extraction import ExtractedPage

# Bumped when the chunks this module produces would differ for the same input.
# ADR-005 §7: a strategy change has to be detectable, so a re-chunk can be
# targeted rather than guessed at.
STRATEGY_VERSION = "section-aware/v1"

# Blocks are joined by a blank line, as `ExtractedPage.text` does.
_BLOCK_SEPARATOR = "\n\n"

# How a reference list numbers its entries: "[12]", "(12)" or "12." at the
# start of a line. Three styles, deterministic; anything else falls back.
_REFERENCE_ENTRY = re.compile(
    r"^[ \t]*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.)\s+\S", re.M
)

# Below this, a reference list has not really been recognised.
_MINIMUM_REFERENCE_ENTRIES = 2


@dataclass(frozen=True)
class _Placed:
    """A run of section text, and the page it came from."""

    start: int
    end: int
    page_number: int


class SectionAwareChunker:
    """Splits a parsed document into chunks that follow its own structure."""

    def __init__(
        self, tokenizer: Tokenizer, *, chunk_size: int, chunk_overlap: int
    ) -> None:
        """
        :param tokenizer: what counts tokens. Its id is recorded on the result,
            because the boundaries depend on it (ADR-0012 §2).
        :param chunk_size: tokens per chunk.
        :param chunk_overlap: tokens shared with the previous chunk. Must be
            less than `chunk_size`, or the windows would not advance.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must not be negative")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        self._tokenizer = tokenizer
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    @property
    def strategy_version(self) -> str:
        return STRATEGY_VERSION

    def chunk(self, pages: Sequence[ExtractedPage]) -> ChunkingResult:
        """Every chunk of a document, in reading order, indexed from zero."""
        chunks: list[Chunk] = []
        for section in detect_sections(pages):
            text, placed = _section_text(section)
            if not text.strip():
                continue
            ranges = (
                self._reference_ranges(text)
                if section.is_references
                else self._window_ranges(text)
            )
            for start, end, token_count in ranges:
                content = text[start:end]
                if not content.strip():
                    continue
                first, last = _pages_covering(placed, start, end)
                chunks.append(
                    Chunk(
                        chunk_index=len(chunks),
                        content=content,
                        section=section.title,
                        page_start=first,
                        page_end=last,
                        token_count=token_count,
                    )
                )
        return ChunkingResult(
            chunks=tuple(chunks),
            strategy_version=STRATEGY_VERSION,
            tokenizer_id=self._tokenizer.id,
        )

    # --- windows --------------------------------------------------------------

    def _window_ranges(self, text: str) -> list[tuple[int, int, int]]:
        """Sliding token windows over one section: `(start, end, token_count)`.

        The last window is whatever remains; it is not padded, and not merged
        into its predecessor, so a short final chunk is possible and is honest.
        """
        tokens = self._tokenizer.tokenize(text)
        if not tokens:
            return []
        step = self._chunk_size - self._chunk_overlap
        ranges: list[tuple[int, int, int]] = []
        start = 0
        while start < len(tokens):
            end = min(start + self._chunk_size, len(tokens))
            ranges.append((tokens[start].start, tokens[end - 1].end, end - start))
            if end == len(tokens):
                break
            start += step
        return ranges

    # --- references -----------------------------------------------------------

    def _reference_ranges(self, text: str) -> list[tuple[int, int, int]]:
        """Whole reference entries, packed up to the budget (ADR-005 §4).

        An entry is never split between chunks. One longer than the budget on
        its own is windowed — the alternative is a chunk no embedding model
        could take. Falls back to ordinary windows when the entry pattern does
        not fire, so a reference list is never dropped for being unusual.
        """
        starts = [match.start() for match in _REFERENCE_ENTRY.finditer(text)]
        if len(starts) < _MINIMUM_REFERENCE_ENTRIES:
            return self._window_ranges(text)

        if starts[0] > 0:
            # A preamble before the first entry — usually the heading itself.
            starts.insert(0, 0)
        bounds = list(zip(starts, starts[1:] + [len(text)]))

        ranges: list[tuple[int, int, int]] = []
        open_start: int | None = None
        open_end = 0
        open_tokens = 0
        for start, end in bounds:
            entry_tokens = self._tokenizer.count(text[start:end])
            if entry_tokens > self._chunk_size:
                if open_start is not None:
                    ranges.append((open_start, open_end, open_tokens))
                    open_start = None
                ranges.extend(
                    (a + start, b + start, count)
                    for a, b, count in self._window_ranges(text[start:end])
                )
                continue
            if open_start is None:
                open_start, open_end, open_tokens = start, end, entry_tokens
            elif open_tokens + entry_tokens <= self._chunk_size:
                open_end, open_tokens = end, open_tokens + entry_tokens
            else:
                ranges.append((open_start, open_end, open_tokens))
                open_start, open_end, open_tokens = start, end, entry_tokens
        if open_start is not None:
            ranges.append((open_start, open_end, open_tokens))
        return [
            (start, end, count)
            for start, end, count in ranges
            if text[start:end].strip()
        ]


def _section_text(section: Section) -> tuple[str, tuple[_Placed, ...]]:
    """One section's text, and which page each part of it came from."""
    parts: list[str] = []
    placed: list[_Placed] = []
    cursor = 0
    for block in section.blocks:
        if parts:
            cursor += len(_BLOCK_SEPARATOR)
            parts.append(_BLOCK_SEPARATOR)
        parts.append(block.text)
        placed.append(_Placed(cursor, cursor + len(block.text), block.page_number))
        cursor += len(block.text)
    return "".join(parts), tuple(placed)


def _pages_covering(placed: Sequence[_Placed], start: int, end: int) -> tuple[int, int]:
    """The first and last page a `[start, end)` range of section text touches."""
    pages = [
        part.page_number for part in placed if part.start < end and part.end > start
    ]
    if not pages:
        # Only possible for a range inside a block separator, which never
        # survives the empty-content check; the nearest page is still correct.
        pages = [placed[0].page_number] if placed else [1]
    return min(pages), max(pages)
