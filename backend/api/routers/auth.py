"""Authentication endpoints."""

from fastapi import APIRouter, Depends
from backend.api.schemas.auth import LoginRequest, LoginResponse, RegisterRequest
from backend.services.auth_service import AuthService

router = APIRouter()


@router.post("/register", response_model=LoginResponse)
async def register(body: RegisterRequest, service: AuthService = Depends()):
    """Register a new user account."""
    ...  # TODO: implement


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, service: AuthService = Depends()):
    """Authenticate and receive a JWT token."""
    ...  # TODO: implement
