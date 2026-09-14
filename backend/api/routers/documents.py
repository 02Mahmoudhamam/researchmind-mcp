"""Document management endpoints.

All four are real: list, get and delete since M2/S2.5, upload since M3/S3.1.
Upload stores and records a document and hands it to ingestion; the document is
`pending` until the ingestion worker (M3/S3.2) exists to process it.

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
from backend.api.schemas.documents import DocumentListResponse, DocumentResponse
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from backend.services.document_service import (
    DocumentService,
    UploadPersistenceFailed,
)
from document_processing.validation import RejectionReason, UploadRejected
from shared.interfaces.storage import StorageError
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


# Which refusals get which status. 413 and 415 are the codes HTTP defines for
# "too large" and "not a type I accept"; everything else about the content —
# empty, unreadable, password-protected, too many pages, no usable filename — is
# a well-formed request for something this service will not take, which is 422.
_REJECTION_STATUS = {
    RejectionReason.TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    RejectionReason.UNSUPPORTED_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
}


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "description": "You already have a live document with this content (ADR-0010)."
        },
        413: {"description": "Larger than MAX_UPLOAD_BYTES."},
        415: {"description": "Not a PDF, judged by its content."},
        422: {
            "description": "Empty, unreadable, password-protected, too many pages, or no usable filename."
        },
        503: {"description": "Document storage is unavailable; nothing was recorded."},
    },
)
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_permission(Permission.DOCUMENT_WRITE)),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """Upload a PDF: 202 with the new `pending` document, or 200 with the one you have.

    202, not 201, as ADR-0009 specifies: the document exists, but what makes it
    useful — parsing and chunking — has only been accepted, not done.

    The owner is the authenticated principal. The request has no field, query
    parameter or header that could name anyone else, and anything sent under
    such a name is not read. The client's `Content-Type` is not read either;
    the bytes decide what the file is.

    The response is the same `DocumentResponse` the other document routes
    return. It does not include where the file is stored.
    """
    try:
        outcome = await service.upload_and_process(
            filename=file.filename, stream=file.file, principal=principal
        )
    except UploadRejected as rejection:
        raise HTTPException(
            status_code=_REJECTION_STATUS.get(
                rejection.reason, status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=rejection.message,
        ) from rejection
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is unavailable. Nothing was saved.",
        ) from exc
    except UploadPersistenceFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The document could not be saved. Nothing was kept.",
        ) from exc

    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _to_response(outcome.document)


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
