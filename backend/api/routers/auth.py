"""Authentication endpoints.

The two public routes in this application: everything else requires a token,
and these are where one comes from. Both stay off the authentication dependency
for that reason — requiring credentials to obtain credentials cannot work — and
both are listed in the public allowlist the route-coverage test reads.

This module is the adapter. It turns the service's domain exceptions into
status codes and its domain types into wire shapes; it makes no security
decision of its own.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.dependencies.services import get_auth_service
from backend.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
)
from backend.security.api_security import unauthenticated
from backend.security.authentication import AuthenticationError
from backend.services.auth_service import AuthService, EmailAlreadyRegistered
from shared.models.user import User

router = APIRouter()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    """Create a new user account.

    201 with the account, and no token. The client signs in separately.

    409 if the address is taken. That does tell the caller whether an email is
    registered — which is unavoidable for a registration endpoint that answers
    synchronously, and is recorded as a known limitation rather than hidden
    behind a 200 that creates nothing. Closing it needs the verification-email
    flow, which is a later sprint.

    A malformed body — short password, bad address, missing name — is FastAPI's
    own 422 from the schema. The policy is not restated here.
    """
    try:
        return await service.register(
            email=body.email, full_name=body.full_name, password=body.password
        )
    except EmailAlreadyRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # The address is the client's own, so this reveals nothing to them
            # that they did not just send. It carries no database detail.
            detail="An account with that email address already exists.",
        ) from exc


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> LoginResponse:
    """Authenticate and receive a JWT access token.

    Every failure — unknown address, no password set, wrong password, disabled
    account — produces the *same* 401 that a forged bearer token produces, from
    the same helper, so the two paths cannot drift apart. The client cannot
    tell which one happened, and neither can a log that only records status
    codes.

    `AuthenticationError.reason` is dropped here. It says which failure it was,
    which is exactly what must not travel.
    """
    try:
        issued = await service.login(email=body.email, password=body.password)
    except AuthenticationError as exc:
        raise unauthenticated() from exc

    return LoginResponse(
        access_token=issued.token,
        expires_in=issued.expires_in_seconds,
    )
