"""Auth request/response schemas.

The endpoints that use these are still stubs — registration and login are
Sprint M2/S2.4. What S2.3 establishes is the *contract*: a password arriving
here is validated and normalised before anything else sees it, so the service
layer S2.4 writes cannot accidentally hash a password nobody checked.
"""

from pydantic import BaseModel, EmailStr

from backend.security.passwords import Password


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


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
