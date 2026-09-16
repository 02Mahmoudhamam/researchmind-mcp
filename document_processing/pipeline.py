"""End-to-end document processing pipeline."""

from pathlib import Path
from shared.models.document import Document
from shared.interfaces.pdf_extraction import PdfTextExtractor
from document_processing.chunker import SectionAwareChunker
from document_processing.embedder import EmbeddingGenerator
from document_processing.metadata_extractor import MetadataExtractor
from vector_db.qdrant.repository import QdrantVectorRepository


class DocumentProcessingPipeline:
    """
    Orchestrates the full document ingestion flow:
    Upload → Parse → Chunk → Embed → Store
    """

    def __init__(self):
        # TODO(M3/3.10): text extraction exists since S3.3 (PyMuPDFTextExtractor),
        # run by the ingestion worker; wire this pipeline to it or retire it.
        self._parser: PdfTextExtractor | None = None
        # TODO(M3/3.10): chunking exists since S3.4 (SectionAwareChunker), run
        # by the ingestion worker; wire this pipeline to it or retire it.
        self._chunker: SectionAwareChunker | None = None
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
