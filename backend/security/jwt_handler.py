"""JWT token creation and verification."""
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from shared.models.user import TokenData, UserRole
from backend.config.settings import get_settings

settings = get_settings()


class JWTHandler:
    """Handles JWT token lifecycle."""

    def create_access_token(
        self,
        user_id: str,
        email: str,
        role: UserRole,
        expires_delta: Optional[timedelta] = None,
    ) -> str:
        """Generate a signed JWT access token."""
        ...  # TODO: implement

    def verify_token(self, token: str) -> TokenData:
        """Decode and validate a JWT token."""
        ...  # TODO: implement

    def refresh_token(self, token: str) -> str:
        """Issue a refreshed token."""
        ...  # TODO: implement
