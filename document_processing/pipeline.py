"""End-to-end document processing pipeline."""

from pathlib import Path
from shared.models.document import Document
from document_processing.pdf_parser import PDFParser
from document_processing.chunker import TextChunker
from document_processing.embedder import EmbeddingGenerator
from document_processing.metadata_extractor import MetadataExtractor
from vector_db.qdrant.repository import QdrantVectorRepository


class DocumentProcessingPipeline:
    """
    Orchestrates the full document ingestion flow:
    Upload → Parse → Chunk → Embed → Store
    """

    def __init__(self):
        self._parser = PDFParser()
        self._chunker = TextChunker()
        self._embedder = EmbeddingGenerator()
        self._extractor = MetadataExtractor()
        self._vector_store = QdrantVectorRepository()

    async def process(self, document: Document, file_path: Path) -> Document:
        """Run a document through the complete processing pipeline."""
        # TODO(M3): wire up parser → chunker → embedder → vector store,
        # advancing the document through shared.models.document.DocumentStatus.
        ...

    async def reprocess(self, document_id: str) -> bool:
        """Re-run processing for an existing document."""
        ...  # TODO: implement
