"""The response for a route that exists but does not work yet.

Several protected routes were scaffolded long before the milestones that give
them behaviour. Until M2/S2.5 their bodies were a bare `...`, which answered in
one of two dishonest ways depending on the route: `200 null` where no response
model was declared — a success status for a request that did nothing — or an
unhandled `ResponseValidationError` where one was, which is a 500.

501 Not Implemented is the status that means what is actually true. It is
raised *inside* the handler, so it is only ever reached after authentication
(401) and authorisation (403) have both passed: an unauthorised caller still
learns nothing, and an authorised one learns the feature is not there yet
rather than that it failed.
"""

from fastapi import HTTPException, status


def not_implemented() -> HTTPException:
    """One message for every unbuilt route, so none of them describes the roadmap."""
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented"
    )
