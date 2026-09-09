"""Authentication and user management service."""
from backend.security.jwt_handler import JWTHandler
from shared.models.user import User, UserRole
from backend.api.schemas.auth import RegisterRequest, LoginRequest, LoginResponse


class AuthService:
    def __init__(self):
        self._jwt = JWTHandler()

    async def register(self, request: RegisterRequest) -> LoginResponse:
        """Create a new user and return JWT."""
        ...  # TODO: hash password, store user, generate token

    async def login(self, request: LoginRequest) -> LoginResponse:
        """Authenticate user credentials and return JWT."""
        ...  # TODO: verify password hash, generate token
