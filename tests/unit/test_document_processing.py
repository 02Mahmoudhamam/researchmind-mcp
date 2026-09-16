"""The chunking stage of the pipeline, at its narrowest.

This file held one `xfail(strict=True)` from the repository foundation onward:
`TextChunker.chunk()` was a stub returning None, and the test was a reminder
that chunking was Milestone M3's. M3/S3.4 implemented it, so — as that test's
own docstring said it would — the reminder becomes a regression test.

It is deliberately the *shape* of the old assertion, against the real chunker:
a long document yields more than one chunk, none empty, indexed contiguously
from zero. Everything else about chunking — sections, references, overlap,
determinism, provenance — is tests/unit/test_chunking.py.
"""

from document_processing.chunker import SectionAwareChunker
from document_processing.tokenization import RegexTokenizer
from shared.models.extraction import ExtractedPage, TextBlock


def test_chunker_produces_chunks_covering_the_document() -> None:
    chunker = SectionAwareChunker(RegexTokenizer(), chunk_size=100, chunk_overlap=20)
    pages = [
        ExtractedPage(
            page_number=1, blocks=(TextBlock(text="word " * 200, font_size=10.0),)
        )
    ]

    result = chunker.chunk(pages)

    assert len(result.chunks) > 1, "a 200-word document must yield more than one chunk"
    assert all(
        chunk.content.strip() for chunk in result.chunks
    ), "no chunk may be empty"
    assert [chunk.chunk_index for chunk in result.chunks] == list(
        range(len(result.chunks))
    ), "chunk_index must be contiguous and ordered from zero"
    assert all(chunk.page_start == 1 and chunk.page_end == 1 for chunk in result.chunks)
