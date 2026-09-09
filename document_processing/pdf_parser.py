"""PDF parsing using PyMuPDF."""
from pathlib import Path
from typing import Optional
from shared.models.document import DocumentMetadata


class PDFParser:
    """Extracts raw text and metadata from PDF files."""

    async def parse(self, file_path: Path) -> tuple[str, DocumentMetadata]:
        """Return (full_text, metadata) from a PDF file."""
        ...  # TODO: use fitz (pymupdf) to open and extract

    async def extract_text(self, file_path: Path) -> str:
        """Extract plain text from all PDF pages."""
        ...  # TODO: implement page iteration

    async def extract_metadata(self, file_path: Path) -> DocumentMetadata:
        """Extract PDF document metadata (title, author, etc.)."""
        ...  # TODO: read doc.metadata
