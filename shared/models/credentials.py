"""What authentication needs to check a password, and nothing more.

This is the one type in the project that carries a credential, and it exists so
that the credential does not have to travel on anything else. `User` is the API
contract and deliberately has no `password_hash` (M2/S2.3); adding one there to
make login convenient would have put a secret on the object every read path
returns.

So the split is:

    UserRepository.get_by_email    -> User             (identity, safe to return)
    UserRepository.get_credentials_by_email -> UserCredentials  (secret, never returned)

Nothing constructs this from a request, and nothing serialises it. A test in
`tests/integration/test_registration_login.py` asserts no route declares it as a
response model.
"""

from pydantic import BaseModel, ConfigDict

from shared.models.user import UserRole


class UserCredentials(BaseModel):
    """An account's stored credential, for verifying a sign-in attempt.

    Frozen, like `Principal`, and for the same reason: nothing downstream of
    the lookup should be able to edit what the database said.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str

    # None for an account that has never had a password set — which, before
    # S2.4, was every account. `verify_password` treats it as a failed
    # verification and still spends the bcrypt cost, so "no password" and
    # "wrong password" are indistinguishable from outside.
    password_hash: str | None

    # Both carried because issuing a token needs them: `create_access_token`
    # takes the role, and login must refuse an inactive account. Nothing else
    # from the row is here — a credential lookup should not become a general
    # user read with a secret attached.
    role: UserRole

    # A plain `bool`, and the contrast with `Principal.is_active` is the point.
    #
    # `Principal` uses `Literal[True]` because it may only ever describe an
    # authenticated, active identity. This type is read *before* that decision
    # is made, so it has to be able to say "inactive" — that is the whole
    # reason login consults it.
    is_active: bool


class AccessToken(BaseModel):
    """A freshly issued access token, as the service hands it back.

    A domain type rather than the API's `LoginResponse`, so that
    `backend/services/` does not import from `backend/api/` — ADR-0001 §2 keeps
    adapter shapes out of the Service Core, and the MCP adapter (ADR-0002) has
    to be able to call the same method without constructing a REST schema. The
    router maps this onto the wire format.
    """

    model_config = ConfigDict(frozen=True)

    token: str

    # Seconds, matching what OAuth 2.0 calls `expires_in` (RFC 6749 §4.2.2).
    # Derived from the same setting the token's `exp` is derived from, so the
    # advertised lifetime cannot drift from the real one.
    expires_in_seconds: int
