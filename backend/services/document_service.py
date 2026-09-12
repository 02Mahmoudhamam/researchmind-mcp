"""Document lifecycle management service."""

from fastapi import UploadFile
from shared.models.document import Document
from document_processing.pipeline import DocumentProcessingPipeline


class DocumentService:
    def __init__(self):
        self._pipeline = DocumentProcessingPipeline()

    async def upload_and_process(self, file: UploadFile, user_id: str) -> Document:
        """Save file, create document record, trigger processing pipeline."""
        ...  # TODO: implement

    async def get_document(self, document_id: str, user_id: str) -> Document | None:
        """Retrieve document by ID with ownership check."""
        ...  # TODO: implement

    async def list_user_documents(self, user_id: str) -> list[Document]:
        """Return all documents owned by a user."""
        ...  # TODO: implement

    async def delete_document(self, document_id: str, user_id: str) -> bool:
        """Delete document, metadata, and vectors."""
        ...  # TODO: implement
