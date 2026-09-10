"""Unit tests for the document processing pipeline.

TextChunker.chunk() is currently a stub (`...`, therefore returning None).
Chunking is implemented in Milestone M3 — Document Ingestion, per
docs/adr/0007-defer-parent-document-retrieval.md, which specifies
section-bounded chunking at roughly 400 tokens with 15% overlap.

The test below is kept and marked xfail(strict=True) rather than deleted or
weakened. Strict means the suite FAILS if it ever starts passing, so the day
M3 implements chunking, this test stops being a reminder and becomes a
regression test with no further action needed.
"""

import pytest

from document_processing.chunker import TextChunker


@pytest.mark.xfail(
    strict=True,
    reason=(
        "TextChunker.chunk() is a stub returning None. Implemented in "
        "Milestone M3 (Document Ingestion); see ADR-0007."
    ),
)
def test_chunker_produces_chunks_covering_the_document():
    chunker = TextChunker(chunk_size=100, chunk_overlap=20)
    text = "word " * 200

    chunks = chunker.chunk(text, document_id="doc-1")

    assert len(chunks) > 1, "a 200-word document must yield more than one chunk"
    assert all(c.document_id == "doc-1" for c in chunks)
    assert all(c.content for c in chunks), "no chunk may be empty"
    assert [c.chunk_index for c in chunks] == list(
        range(len(chunks))
    ), "chunk_index must be contiguous and ordered from zero"
