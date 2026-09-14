"""The authenticated identity.

A ``Principal`` is what the server concluded after verifying a token and
resolving its subject against the database. It is deliberately *not* a ``User``:
``User`` is the API contract for a row that happens to exist, and one can be
built from a request body, a fixture, or any JSON that has the right shape. A
``Principal`` asserts something stronger — *this request was authenticated* —
and only the resolver in ``backend/security/authentication.py`` may say so.

Keeping the two types apart is what makes ``docs/security/principles.md`` §2
checkable: "no service method accepts a caller-supplied ``user_id``". A method
that takes a ``Principal`` cannot be handed one, because the type does not
admit it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from shared.models.user import UserRole


class Principal(BaseModel):
    """An identity the server has authenticated.

    Four fields, no more. Everything here is something an authorisation
    decision needs; anything else about the user belongs in a lookup, not in
    the object that gets passed down every call path.
    """

    # Frozen. A request's identity is settled once, before the handler runs.
    # If it were mutable, "who is this request" would depend on where in the
    # handler you asked — and the answer M2/S2.5 will feed to the SQL ownership
    # predicate must be the same answer authentication produced.
    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str

    # From the database row, never from the token — see the resolver for why.
    role: UserRole

    # ``Literal[True]``, not ``bool``, and this is the point of the type.
    #
    # principles.md §1 requires that a structurally valid token whose subject is
    # not an *active* user be rejected. Spelled ``is_active: bool`` that rule
    # lives in whoever remembers to check it. Spelled ``Literal[True]`` it lives
    # in the type: ``Principal(is_active=user.is_active)`` is a mypy error,
    # because ``User.is_active`` is a plain ``bool``, so the resolver is forced
    # to branch on it explicitly before it can construct one. Pydantic rejects
    # ``False`` at runtime for anything that gets here without mypy having run.
    #
    # An inactive principal is therefore not merely never built. It is
    # unrepresentable.
    is_active: Literal[True] = True
