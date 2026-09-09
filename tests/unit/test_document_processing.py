"""Unit tests for document processing modules."""
import pytest
from document_processing.chunker import TextChunker


def test_chunker_produces_chunks():
    chunker = TextChunker(chunk_size=100, chunk_overlap=20)
    text = "word " * 200
    chunks = chunker.chunk(text, document_id="doc-1")
    assert len(chunks) > 1
    assert all(c.document_id == "doc-1" for c in chunks)
