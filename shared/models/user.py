"""User and auth models."""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
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
    user_id: str
    email: str
    role: UserRole
    exp: Optional[datetime] = None
