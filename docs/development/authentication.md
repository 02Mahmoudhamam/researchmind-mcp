# Authentication

```
HTTP request
      │  Authorization: Bearer <token>
      ▼
HTTPBearer(auto_error=False)        ← parses the header, decides nothing
      │  HTTPAuthorizationCredentials | None
      ▼
get_current_user                    ← the HTTP adapter: 401, and the challenge
      │  backend/security/api_security.py
      ▼
resolve_principal                   ← the decision. No FastAPI here.
      │  backend/security/authentication.py
      ├── JWTHandler.verify_token    signature, exp, sub, algorithm
      ├── UserRepository.get_by_id   does the subject still exist?
      └── user.is_active             is the account still in service?
      ▼
Principal                           ← or AuthenticationError. Never None.
```

## The contract

| Question | Answer |
|---|---|
| What does `get_current_user` return? | A `Principal`. Always. |
| What does it return on failure? | Nothing — it **raises** `HTTPException(401)` |
| Is `Optional[Principal]` allowed? | **No.** Asserted by `tests/unit/test_architecture.py` |
| Which routes are protected? | All of them, except an explicit allowlist |
| Where is the allowlist? | `tests/integration/test_authentication.py::PUBLIC_PATHS` |
| Authentication failure | **401** `Could not validate credentials` |
| Authorisation failure | **403** — reserved for S2.5, not yet emitted |
| Can a route see *why* authentication failed? | No. It never runs. |
| Can a client? | No. Every failure is byte-identical. |
| Does authentication write? | No. It reads, and never commits. |

## Why the decision is not in the FastAPI layer

ADR-0002 §6: *"Both adapters resolve identity through the same resolver, so MCP
cannot become a path around REST authentication."*

`resolve_principal` therefore imports no FastAPI and knows nothing about HTTP.
`api_security.py` is a thin adapter that supplies the token and translates the
failure into a status code. When the MCP adapter needs identity it calls the
same function and translates the same error its own way, rather than
reimplementing the checks — which is how two adapters end up with two
authentication behaviours and one of them wrong.

## Why a `Principal` and not a `User`

They have the same fields and opposite meanings.

A `User` describes a row that exists. It can be constructed from a request body,
a fixture, or any JSON of the right shape — which is precisely the identity
source [principles.md §2](../security/principles.md) forbids. A `Principal`
asserts that *this request was authenticated*, and only `resolve_principal` may
say so.

That distinction is what makes "no service method accepts a caller-supplied
`user_id`" a property of the type system rather than a rule in a document.

`Principal.is_active` is `Literal[True]`, so `Principal(is_active=user.is_active)`
is a mypy error — the resolver is *forced* to branch on it before it can build
one. An inactive principal is not merely never built; it cannot be represented.

## Why the database is consulted on every request

A valid signature proves when a token was minted. It does not prove the account
is still in service.

There is no revocation store in the MVP ([principles.md §6](../security/principles.md)),
so this lookup is the only thing that can end a session before its expiry. A user
deactivated or deleted after a token was issued is rejected on their next
request, not sixty minutes later.

The lookup happens **after** signature verification, so a forged token never
costs a query.

## Why role and email come from the row, not the token

A token issued before a demotion still carries the old role. Honouring it would
leave an ex-administrator privileged until expiry — for exactly the reason
honouring a deactivated user's token would leave them signed in.

The token answers *who do you claim to be*. PostgreSQL, the sole authority
([principles.md §3](../security/principles.md)), answers *and what are you now*.

Only `sub` is actually consumed from the token.

## 401 or 403

| | |
|---|---|
| **401 Unauthorized** | We do not know who you are. Missing, malformed, forged, expired, tampered, wrong-secret, unknown subject, deleted user, inactive user. |
| **403 Forbidden** | We know who you are, and you may not do this. |

Nothing emits 403 yet. RBAC is **S2.5**; `require_role` is still a stub.

`HTTPBearer` is configured `auto_error=False` for this reason. Left to itself it
raises 403 for a missing header — the right refusal under the wrong code, decided
inside a header parser that has not seen the token. Returning `None` instead puts
every authentication failure in one function.

## What a failure tells the client

Nothing beyond "no".

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer

{"detail": "Could not validate credentials"}
```

Identical for every cause. An error that distinguishes *unknown user* from *bad
token* from *disabled account* is an account-enumeration oracle
([principles.md §7](../security/principles.md)).

The reason survives internally on `AuthenticationError.reason`, for logs and for
the test that asserts the categories are distinguishable inside and identical
outside.

## Adding a protected route

Nothing to do. Routes are protected by default:

```python
@router.get("/things")
async def list_things(principal: Principal = Depends(get_current_user)):
    ...
```

A route added *without* that dependency fails
`test_every_non_public_route_requires_authentication`, which reads the
application's own dependency tree. Making a route public means adding it to
`PUBLIC_PATHS` — deliberately an edit that shows up in review.

## What S2.2 did not do

No passwords, no `password_hash`, no registration, no login, no RBAC, no
migration. There is currently **no way to obtain a token over HTTP** — S2.4 adds
that. Tokens are minted directly from a user id, which is why authentication
could be fixed before login exists, and why fixing it first was the right order.
