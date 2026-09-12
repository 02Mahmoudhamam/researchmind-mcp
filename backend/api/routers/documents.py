"""Document management endpoints."""

from fastapi import APIRouter, Depends, UploadFile, File
from backend.api.dependencies.services import get_document_service
from backend.api.schemas.documents import DocumentResponse, DocumentListResponse
from backend.services.document_service import DocumentService
from backend.security.api_security import get_current_user
from shared.models.user import User

router = APIRouter()


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
):
    """Upload and begin processing a research document."""
    ...  # TODO: implement


@router.get("/", response_model=DocumentListResponse)
async def list_documents(
    user: User = Depends(get_current_user),
    service: DocumentService = Depends(get_document_service),
):
    """List all documents for the current user."""
    ...  # TODO: implement


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: str, user: User = Depends(get_current_user)):
    """Retrieve a specific document."""
    ...  # TODO: implement


@router.delete("/{document_id}")
async def delete_document(document_id: str, user: User = Depends(get_current_user)):
    """Delete a document and its embeddings."""
    ...  # TODO: implement
