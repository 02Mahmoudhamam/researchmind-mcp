"""Auth request/response schemas.

A password arriving here is validated and normalised before anything else sees
it (M2/S2.3), so the service cannot hash one nobody checked.

Nothing in this module can carry a credential outward. `RegisterResponse` lists
its fields explicitly rather than echoing the model it is built from, so a
column added to `User` later cannot appear in a response by default — which is
the mistake that leaks a hash.
"""

from datetime import datetime

from pydantic import BaseModel, EmailStr

from backend.security.passwords import Password
from shared.models.user import UserRole


class RegisterRequest(BaseModel):
    email: EmailStr
    # The policy applies on the way *in*, where a password is chosen. The field
    # holds the NFKC-normalised form, because that is what will be hashed.
    password: Password
    full_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    # A plain `str`, and the asymmetry with RegisterRequest is deliberate.
    #
    # Validating a sign-in attempt against the policy would reject a password
    # that was legitimately chosen under an older, laxer one — locking out the
    # user instead of letting them in and prompting a change. It would also
    # answer "too short" where it should answer "wrong", which tells an
    # attacker something about the account they are guessing at.
    #
    # `verify_password` normalises the candidate itself, so a login still
    # matches a password typed in a different Unicode form.
    password: str


class RegisterResponse(BaseModel):
    """The new account, as the client is allowed to see it.

    Registration returns the account and **not** a token: creating an account
    and proving you can sign into it are separate acts, and keeping them apart
    is what lets a verification step slot in later without changing the
    contract. The client calls `/auth/login` next.

    An explicit field list, not `User` itself. FastAPI filters the response
    through this model, so this is the second of two independent reasons a
    `password_hash` cannot reach a client — the first being that `User` has no
    such field at all.
    """

    id: str
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class LoginResponse(BaseModel):
    """The token, in the shape OAuth 2.0 describes (RFC 6749 §4.2.2).

    Unchanged by S2.4 — `expires_in` is seconds, and there is no
    `refresh_token` field because there is no refresh token
    (security/principles.md §6).
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
