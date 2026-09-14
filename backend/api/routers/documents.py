"""Document management endpoints.

Three of the four are real as of M2/S2.5: list, get and delete are wired to the
`DocumentService` M1 built. Upload is ingestion, which is M3, and answers 501.

Every route passes three separate checks, in this order, and each has its own
answer:

    authentication   who is this?            401   get_current_user
    authorisation    may this role do it?    403   require_permission
    ownership        is this row theirs?     404   the repository's SQL predicate

They are deliberately not collapsed. A permission check cannot stand in for
ownership — a researcher may read documents, but not *yours* — and ownership
cannot stand in for permission, because a viewer owns documents they may not
delete. Authorisation is decided from the role alone, before any row is looked
up, so a 403 says nothing about whether the document exists.

The route never reads an identity from the request. The only `Principal` in
scope is the one the dependency produced, and it is handed to the service
whole.
"""

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)

from backend.api.dependencies.services import get_document_service
from backend.api.not_implemented import not_implemented
from backend.api.schemas.documents import DocumentListResponse, DocumentResponse
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from backend.services.document_service import DocumentService
from shared.models.document import Document
from shared.models.principal import Principal

router = APIRouter()

# The same answer for "no such document", "someone else's document" and
# "already deleted". The repository already makes those indistinguishable
# (M1/S1.3); a route that returned 403 for a foreign document would reopen the
# existence oracle the SQL predicate exists to close.
_NOT_FOUND = "Document not found"


def _to_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        metadata=document.metadata.model_dump(mode="json"),
    )


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    principal: Principal = Depends(require_permission(Permission.DOCUMENT_WRITE)),
) -> DocumentResponse:
    """Upload and begin processing a research document — M3.

    Authorised now so that the permission it will need is settled before its
    body exists. Ingestion — storage (ADR-0008), parsing, chunking, the job
    queue (ADR-0009) — is Milestone M3.
    """
    raise not_implemented()


@router.get("/", response_model=DocumentListResponse)
async def list_documents(
    principal: Principal = Depends(require_permission(Permission.DOCUMENT_READ)),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    """List the caller's documents, newest first, excluding deleted ones.

    Always the caller's. There is no parameter that selects whose documents to
    list, and a `?user_id=` added to the request is ignored — the list is
    scoped by the Principal, and the Principal came from the token.
    """
    documents = await service.list_user_documents(principal)
    return DocumentListResponse(
        documents=[_to_response(document) for document in documents],
        total=len(documents),
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    principal: Principal = Depends(require_permission(Permission.DOCUMENT_READ)),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """Retrieve one of the caller's documents, or 404."""
    document = await service.get_document(document_id, principal)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return _to_response(document)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_document(
    document_id: str,
    principal: Principal = Depends(require_permission(Permission.DOCUMENT_WRITE)),
    service: DocumentService = Depends(get_document_service),
) -> Response:
    """Soft-delete one of the caller's documents: 204, or 404.

    The PostgreSQL half of ADR-0003's deletion order, committed by the service.
    Removing the document's vectors from Qdrant is the second half and belongs
    to M4 — and until M3 ingests something, a document has no vectors to
    remove.
    """
    if not await service.delete_document(document_id, principal):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
