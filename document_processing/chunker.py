"""Text chunking strategies for embedding."""
from typing import List
from shared.models.document import DocumentChunk
from shared.utils.id_generator import generate_id


class TextChunker:
    """Splits documents into overlapping chunks for embedding."""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, text: str, document_id: str) -> List[DocumentChunk]:
        """Split text into DocumentChunk objects."""
        ...  # TODO: use RecursiveCharacterTextSplitter

    def chunk_by_section(self, text: str, document_id: str) -> List[DocumentChunk]:
        """Split by detected section headers."""
        ...  # TODO: implement heading-aware chunking
