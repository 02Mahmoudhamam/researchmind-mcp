"""Semantic search endpoints — M4/S4.2."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.dependencies.services import get_search_service
from backend.api.schemas.search import SearchHit, SearchRequest, SearchResponse
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from backend.services.search_service import (
    InvalidSearchQuery,
    SearchService,
    SearchUnavailable,
)
from shared.models.principal import Principal
from shared.models.retrieval import RetrievalQuery

router = APIRouter()

# Fixed sentences, written here. The service's exceptions already carry no
# host, credential, query text or driver message (ADR-0014 §6), and this is the
# second place that is true: `principles.md` §7 requires error responses to
# carry no internal detail, so none of these is built from an exception.
_UNAVAILABLE = "Search is unavailable. No results were returned."


@router.post("/", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest,
    principal: Annotated[
        Principal, Depends(require_permission(Permission.SEARCH_QUERY))
    ],
    service: Annotated[SearchService, Depends(get_search_service)],
) -> SearchResponse:
    """Search the caller's own corpus.

    Thin on purpose. Retrieval — embedding the query, filtering in Qdrant,
    re-validating every candidate against PostgreSQL — is `SearchService`'s,
    and none of it is repeated here. This route turns a request into a
    `RetrievalQuery`, hands it the authenticated identity, and narrows the
    answer to what a client may see.

    **The owner is `principal`, and there is nowhere else it could come from.**
    `SearchRequest` has no owner field and forbids extras, so a body naming
    another user is a 422 rather than a privilege escalation, and the
    `document_ids` a caller may send can only narrow a search the owner filter
    has already scoped (ADR-0014 §1).
    """
    query = RetrievalQuery(
        text=body.query,
        top_k=body.top_k,
        score_threshold=body.score_threshold,
        document_ids=None if body.document_ids is None else tuple(body.document_ids),
    )

    try:
        results = await service.search(query, principal)
    except InvalidSearchQuery as exc:
        # The caller's fault and fixable by them. `str(exc)` is safe by
        # construction — the service builds these messages without the query
        # text, and a test asserts it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except SearchUnavailable as exc:
        # Not an empty result (ADR-0014 §6). 503 is the code COMPLETION_PLAN's
        # Phase 5 DoD already specified for this endpoint.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE
        ) from exc

    hits = [SearchHit.of(result) for result in results]
    return SearchResponse(results=hits, total=len(hits))
