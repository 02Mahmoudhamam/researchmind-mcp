"""Tokenizing, section detection and chunking — M3/S3.4. No database, no mocks.

Everything here is pure: pages in, sections or chunks out. What ingestion *does*
with a chunking result — claims, statuses, persistence, recovery — is
tests/integration/test_chunk_ingestion.py.
"""

import inspect

import pytest

from document_processing.chunker import STRATEGY_VERSION, SectionAwareChunker
from document_processing.sections import (
    MAX_HEADING_WORDS,
    body_font_size,
    detect_sections,
    is_heading,
)
from document_processing.tokenization import TOKENIZER_ID, RegexTokenizer
from shared.interfaces.tokenization import Tokenizer
from shared.models.extraction import ExtractedPage, TextBlock

BODY = "Sentence about the subject matter at hand and what follows from it. "


def _page(number: int, *blocks: tuple[str, float]) -> ExtractedPage:
    return ExtractedPage(
        page_number=number,
        blocks=tuple(TextBlock(text=text, font_size=size) for text, size in blocks),
    )


def _chunker(size: int = 60, overlap: int = 12) -> SectionAwareChunker:
    return SectionAwareChunker(RegexTokenizer(), chunk_size=size, chunk_overlap=overlap)


# =============================================================================
# The tokenizer
# =============================================================================


class TestRegexTokenizer:
    def test_it_identifies_itself(self) -> None:
        """Every chunk records this, so M4 can find what to re-chunk."""
        assert RegexTokenizer().id == TOKENIZER_ID == "regex-word/v1"

    @pytest.mark.parametrize(
        ("text", "tokens"),
        [
            ("hello world", ["hello", "world"]),
            ("Hello, world!", ["Hello", ",", "world", "!"]),
            ("don't stop", ["don't", "stop"]),
            ("one  two\tthree\nfour", ["one", "two", "three", "four"]),
            ("ISBN 978-3-16", ["ISBN", "978", "-", "3", "-", "16"]),
            ("", []),
            ("   \n\t ", []),
        ],
        ids=["words", "punct", "apostrophe", "whitespace", "digits", "empty", "blank"],
    )
    def test_what_counts_as_a_token(self, text: str, tokens: list[str]) -> None:
        tokenizer = RegexTokenizer()

        spans = tokenizer.tokenize(text)

        assert [text[span.start : span.end] for span in spans] == tokens
        assert tokenizer.count(text) == len(tokens)

    def test_cjk_characters_are_counted_one_by_one(self) -> None:
        """A WordPiece vocabulary splits these per character; so does this."""
        assert RegexTokenizer().count("研究論文の要旨") == 7

    def test_spans_are_ordered_and_never_overlap(self) -> None:
        spans = RegexTokenizer().tokenize(f"{BODY}{BODY}")

        assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))
        assert all(span.start < span.end for span in spans)

    def test_the_same_text_always_tokenizes_the_same_way(self) -> None:
        tokenizer = RegexTokenizer()

        assert tokenizer.tokenize(BODY) == tokenizer.tokenize(BODY)

    def test_count_agrees_with_tokenize(self) -> None:
        tokenizer = RegexTokenizer()
        text = f"{BODY} 研究 don't — stop."

        assert tokenizer.count(text) == len(tokenizer.tokenize(text))

    def test_it_satisfies_the_protocol_the_chunker_depends_on(self) -> None:
        """And the protocol stays the minimum the chunker uses."""
        assert set(vars(Tokenizer)) >= {"tokenize", "count"}
        assert list(inspect.signature(Tokenizer.tokenize).parameters) == [
            "self",
            "text",
        ]
        assert list(inspect.signature(Tokenizer.count).parameters) == ["self", "text"]
        tokenizer: Tokenizer = RegexTokenizer()
        assert tokenizer.count("a b") == 2


# =============================================================================
# Section detection
# =============================================================================


class TestHeadingDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "1 Introduction",
            "2. Related Work",
            "3.1 Evaluation",
            "4.2.1 Ablations",
            "IV. Results",
            "Abstract",
            "Introduction",
            "Related Work",
            "METHODS",
            "Results:",
            "5 Discussion",
            "References",
            "Bibliography",
            "Acknowledgements",
            "Appendix A",
        ],
        ids=lambda value: value,
    )
    def test_headings_at_body_size_are_found_by_pattern(self, text: str) -> None:
        assert is_heading(text, 10.0, body_size=10.0)

    @pytest.mark.parametrize(
        "text",
        [
            "We show that things are things and that this matters a great deal.",
            "The method is described below.",
            "and then the result follows",
            "see Table 3 for the full breakdown of results across every dataset",
        ],
        ids=["long", "sentence", "fragment", "reference-to-table"],
    )
    def test_prose_is_not_a_heading(self, text: str) -> None:
        assert not is_heading(text, 10.0, body_size=10.0)

    def test_a_block_of_more_than_one_line_is_never_a_heading(self) -> None:
        assert not is_heading("1 Introduction\nand more text", 20.0, body_size=10.0)

    def test_a_long_title_in_large_type_is_still_not_a_heading(self) -> None:
        words = " ".join(f"word{i}" for i in range(MAX_HEADING_WORDS + 1))

        assert not is_heading(words, 20.0, body_size=10.0)

    def test_visibly_larger_type_is_a_heading_without_a_pattern(self) -> None:
        """How an unnumbered or non-English heading is found (ADR-0012 §3)."""
        assert is_heading("研究の背景", 13.0, body_size=10.0)
        assert is_heading("Something Particular", 12.0, body_size=10.0)
        assert not is_heading("Something Particular", 10.5, body_size=10.0)

    def test_large_type_that_ends_a_sentence_is_prose(self) -> None:
        assert not is_heading("This is a sentence.", 14.0, body_size=10.0)

    def test_the_body_size_is_what_most_characters_are_set_in(self) -> None:
        pages = [
            _page(1, ("Title", 24.0), (BODY * 3, 10.0), ("1 Introduction", 13.0)),
            _page(2, (BODY * 3, 10.0)),
        ]

        assert body_font_size(pages) == 10.0

    def test_a_document_with_no_text_has_no_body_size(self) -> None:
        assert body_font_size([_page(1)]) == 0.0


class TestSectionDetection:
    def test_sections_run_from_heading_to_heading(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY, 10.0), ("2 Method", 13.0)),
            _page(2, (BODY, 10.0), ("3 Results", 13.0), (BODY, 10.0)),
        ]

        sections = detect_sections(pages)

        assert [section.title for section in sections] == [
            "1 Introduction",
            "2 Method",
            "3 Results",
        ]
        assert [len(section.blocks) for section in sections] == [2, 2, 2]

    def test_the_heading_stays_with_what_it_introduces(self) -> None:
        sections = detect_sections([_page(1, ("1 Introduction", 13.0), (BODY, 10.0))])

        assert sections[0].blocks[0].text == "1 Introduction"

    def test_text_before_the_first_heading_has_no_title(self) -> None:
        pages = [_page(1, (BODY, 10.0), ("1 Introduction", 13.0), (BODY, 10.0))]

        sections = detect_sections(pages)

        assert [section.title for section in sections] == [None, "1 Introduction"]

    def test_a_document_with_no_headings_is_one_untitled_section(self) -> None:
        pages = [_page(1, (BODY, 10.0)), _page(2, (BODY, 10.0))]

        (section,) = detect_sections(pages)

        assert section.title is None
        assert [block.page_number for block in section.blocks] == [1, 2]

    def test_a_section_carries_the_page_each_block_came_from(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY, 10.0)),
            _page(2, (BODY, 10.0)),
        ]

        (section,) = detect_sections(pages)

        assert [block.page_number for block in section.blocks] == [1, 1, 2]

    def test_empty_pages_contribute_nothing_and_break_nothing(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY, 10.0)),
            _page(2),
            _page(3, (BODY, 10.0)),
        ]

        (section,) = detect_sections(pages)

        assert [block.page_number for block in section.blocks] == [1, 1, 3]

    def test_no_text_means_no_sections(self) -> None:
        assert detect_sections([_page(1), _page(2)]) == ()

    @pytest.mark.parametrize(
        ("title", "is_references"),
        [
            ("References", True),
            ("REFERENCES", True),
            ("6 References", True),
            ("Bibliography", True),
            ("Works Cited", True),
            ("1 Introduction", False),
            ("Referenced Architectures", False),
        ],
        ids=lambda value: str(value),
    )
    def test_a_reference_section_is_recognised_by_its_title(
        self, title: str, is_references: bool
    ) -> None:
        pages = [_page(1, (title, 13.0), (BODY, 10.0))]

        (section,) = detect_sections(pages)

        assert section.is_references is is_references


# =============================================================================
# Chunking
# =============================================================================


class TestChunking:
    def test_a_short_section_is_one_chunk(self) -> None:
        pages = [_page(1, ("1 Introduction", 13.0), (BODY, 10.0))]

        result = _chunker().chunk(pages)

        assert len(result.chunks) == 1
        assert result.chunks[0].section == "1 Introduction"
        assert result.chunks[0].content.startswith("1 Introduction")

    def test_a_long_section_becomes_several_windows(self) -> None:
        pages = [_page(1, ("1 Introduction", 13.0), (BODY * 20, 10.0))]

        result = _chunker(size=60, overlap=12).chunk(pages)

        assert len(result.chunks) > 3
        assert all(chunk.token_count <= 60 for chunk in result.chunks)
        assert [chunk.chunk_index for chunk in result.chunks] == list(
            range(len(result.chunks))
        )

    def test_consecutive_chunks_of_a_section_overlap_by_the_configured_tokens(
        self,
    ) -> None:
        tokenizer = RegexTokenizer()
        pages = [_page(1, (BODY * 20, 10.0))]

        chunks = _chunker(size=50, overlap=10).chunk(pages).chunks

        first, second = chunks[0], chunks[1]
        tail = tokenizer.tokenize(first.content)[-10:]
        shared = first.content[tail[0].start : tail[-1].end]
        assert second.content.startswith(shared)

    def test_no_overlap_when_none_is_configured(self) -> None:
        pages = [_page(1, (BODY * 20, 10.0))]

        chunks = _chunker(size=50, overlap=0).chunk(pages).chunks

        rebuilt = "".join(chunk.content for chunk in chunks)
        assert rebuilt.replace(" ", "") == pages[0].text.replace(" ", "").replace(
            "\n", ""
        )

    def test_chunks_never_cross_a_section_boundary(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY * 3, 10.0)),
            _page(2, ("2 Method", 13.0), (BODY * 3, 10.0)),
        ]

        chunks = _chunker(size=400, overlap=40).chunk(pages).chunks

        assert [chunk.section for chunk in chunks] == ["1 Introduction", "2 Method"]
        assert "2 Method" not in chunks[0].content
        assert "1 Introduction" not in chunks[1].content

    def test_a_chunk_records_the_pages_it_covers(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY * 2, 10.0)),
            _page(2, (BODY * 2, 10.0)),
        ]

        chunks = _chunker(size=400, overlap=40).chunk(pages).chunks

        assert (chunks[0].page_start, chunks[0].page_end) == (1, 2)

    def test_a_chunk_inside_one_page_starts_and_ends_there(self) -> None:
        pages = [_page(1, (BODY, 10.0)), _page(2, ("2 Method", 13.0), (BODY, 10.0))]

        chunks = _chunker(size=400, overlap=40).chunk(pages).chunks

        assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]

    def test_content_is_a_verbatim_substring_of_the_page_text(self) -> None:
        """Nothing is re-joined or re-normalised: a citation can be found again."""
        pages = [_page(1, ("1 Introduction", 13.0), (BODY * 4, 10.0))]
        whole = "\n\n".join(block.text for block in pages[0].blocks)

        for chunk in _chunker(size=40, overlap=8).chunk(pages).chunks:
            assert chunk.content in whole

    def test_the_token_count_is_the_tokenizer_s_count_of_the_content(self) -> None:
        tokenizer = RegexTokenizer()
        pages = [_page(1, (BODY * 10, 10.0))]

        for chunk in _chunker(size=40, overlap=8).chunk(pages).chunks:
            assert chunk.token_count == tokenizer.count(chunk.content)

    def test_empty_pages_produce_no_chunks_and_shift_nothing(self) -> None:
        pages = [_page(1, (BODY, 10.0)), _page(2), _page(3, (BODY, 10.0))]

        chunks = _chunker(size=400, overlap=40).chunk(pages).chunks

        assert len(chunks) == 1
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 3)

    def test_a_document_with_no_text_produces_no_chunks(self) -> None:
        result = _chunker().chunk([_page(1), _page(2)])

        assert result.chunks == ()
        assert result.strategy_version == STRATEGY_VERSION

    def test_unicode_survives_chunking(self) -> None:
        text = "研究論文の要旨。" * 30
        pages = [_page(1, ("研究の背景", 13.0), (text, 10.0))]

        chunks = _chunker(size=40, overlap=8).chunk(pages).chunks

        assert chunks[0].section == "研究の背景"
        assert "研究論文" in "".join(chunk.content for chunk in chunks)

    def test_the_result_records_the_strategy_and_the_tokenizer(self) -> None:
        result = _chunker().chunk([_page(1, (BODY, 10.0))])

        assert result.strategy_version == "section-aware/v1"
        assert result.tokenizer_id == "regex-word/v1"

    def test_the_same_pages_always_produce_the_same_chunks(self) -> None:
        """ADR-0012 §8: determinism is a property, not an aspiration."""
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY * 8, 10.0)),
            _page(2, ("References", 13.0), ("[1] A. Author, A paper, 2020.", 9.0)),
        ]
        chunker = _chunker(size=40, overlap=8)

        first = chunker.chunk(pages)
        second = _chunker(size=40, overlap=8).chunk(pages)

        assert first == second

    @pytest.mark.parametrize(
        ("size", "overlap"),
        [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
        ids=["zero", "negative", "negative-overlap", "equal", "larger"],
    )
    def test_a_configuration_that_would_not_advance_is_refused(
        self, size: int, overlap: int
    ) -> None:
        with pytest.raises(ValueError):
            SectionAwareChunker(
                RegexTokenizer(), chunk_size=size, chunk_overlap=overlap
            )


# =============================================================================
# References
# =============================================================================


def _reference_list(style: str, count: int = 6) -> str:
    marker = {"bracket": "[{}]", "paren": "({})", "dotted": "{}."}[style]
    return "\n".join(
        f"{marker.format(i)} A. Author and B. Author, A paper about things "
        f"number {i}, Journal of Things, 20{i:02d}."
        for i in range(1, count + 1)
    )


class TestReferences:
    @pytest.mark.parametrize("style", ["bracket", "paren", "dotted"])
    def test_entries_are_never_split_between_chunks(self, style: str) -> None:
        pages = [_page(1, ("References", 13.0), (_reference_list(style), 9.0))]

        chunks = _chunker(size=40, overlap=8).chunk(pages).chunks

        assert len(chunks) > 1, "the list is longer than one chunk"
        for chunk in chunks:
            for line in chunk.content.splitlines():
                if line.strip() and line.strip()[0] in "[(0123456789":
                    assert line.rstrip().endswith("."), line

    def test_every_entry_survives_somewhere(self) -> None:
        pages = [_page(1, ("References", 13.0), (_reference_list("bracket"), 9.0))]

        chunks = _chunker(size=40, overlap=8).chunk(pages).chunks

        rebuilt = "\n".join(chunk.content for chunk in chunks)
        for i in range(1, 7):
            assert f"[{i}] A. Author" in rebuilt

    def test_reference_chunks_do_not_overlap(self) -> None:
        pages = [_page(1, ("References", 13.0), (_reference_list("bracket"), 9.0))]

        chunks = _chunker(size=40, overlap=8).chunk(pages).chunks

        first_entry_of_second = chunks[1].content.splitlines()[0]
        assert first_entry_of_second not in chunks[0].content

    def test_a_multi_line_entry_stays_whole(self) -> None:
        entries = "\n".join(
            f"[{i}] A. Author, B. Author and C. Author,\n    A paper number {i},\n"
            f"    Journal of Things, 2020."
            for i in range(1, 5)
        )
        pages = [_page(1, ("References", 13.0), (entries, 9.0))]

        chunks = _chunker(size=60, overlap=0).chunk(pages).chunks

        for i in range(1, 5):
            holder = [c for c in chunks if f"[{i}] A. Author" in c.content]
            assert holder, i
            assert f"A paper number {i}" in holder[0].content

    def test_an_entry_longer_than_the_budget_is_windowed_on_its_own(self) -> None:
        long_entry = "[1] " + BODY * 10
        pages = [_page(1, ("References", 13.0), (f"{long_entry}\n[2] Short one.", 9.0))]

        chunks = _chunker(size=30, overlap=0).chunk(pages).chunks

        assert len(chunks) > 2
        assert all(chunk.token_count <= 30 for chunk in chunks)
        assert any("[2] Short one." in chunk.content for chunk in chunks)

    def test_an_unrecognised_list_falls_back_to_windows_and_is_never_dropped(
        self,
    ) -> None:
        entries = "\n".join(
            f"Author, A. ({2000 + i}). A paper about things number {i}. Journal."
            for i in range(1, 8)
        )
        pages = [_page(1, ("References", 13.0), (entries, 9.0))]

        chunks = _chunker(size=40, overlap=8).chunk(pages).chunks

        assert chunks, "a reference list is never discarded"
        rebuilt = " ".join(chunk.content for chunk in chunks)
        for i in range(1, 8):
            assert f"number {i}" in rebuilt

    def test_a_references_section_is_still_bounded_by_its_section(self) -> None:
        pages = [
            _page(1, ("1 Introduction", 13.0), (BODY * 2, 10.0)),
            _page(2, ("References", 13.0), (_reference_list("bracket", 3), 9.0)),
        ]

        chunks = _chunker(size=400, overlap=40).chunk(pages).chunks

        assert [chunk.section for chunk in chunks] == ["1 Introduction", "References"]
