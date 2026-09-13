"""User and auth models."""

from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    RESEARCHER = "researcher"
    VIEWER = "viewer"


class User(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: UserRole = UserRole.RESEARCHER
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TokenData(BaseModel):
    """The claims of a token that has already been verified.

    `exp` is required, not optional (security/principles.md §1). Optional would
    let a successfully-verified token exist with no expiry, which is precisely
    the token that never stops being valid — and with no server-side revocation
    in the MVP, expiry is the only thing that ends a session.
    """

    user_id: str
    email: str
    role: UserRole
    exp: datetime
