"""JWT token creation and verification."""

from datetime import timedelta
from typing import Optional
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
        # TODO(M2): sign with jose.jwt using settings.SECRET_KEY/ALGORITHM;
        # expiry from datetime.utcnow() + (expires_delta or the configured default).
        ...

    def verify_token(self, token: str) -> TokenData:
        """Decode and validate a JWT token."""
        # TODO(M2): jose.jwt.decode, catching jose.JWTError and failing CLOSED.
        # See SECURITY.md: this currently verifies nothing.
        ...

    def refresh_token(self, token: str) -> str:
        """Issue a refreshed token."""
        ...  # TODO: implement
