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

## Getting a token

Two public endpoints, added in M2/S2.4. They are the only routes that do not
require one.

```
POST /api/v1/auth/register   201  ->  the account. No token.
POST /api/v1/auth/login      200  ->  {"access_token", "token_type", "expires_in"}
```

**Registration does not sign you in.** Creating an account and proving you can
supply its password are separate acts, and keeping them apart is what lets an
email-verification step slot in later without changing this contract.

Uniqueness is the database's decision, not the service's. There is no "does
this email exist" query before the insert: between that check and the insert
another request can create the same account, and only the `uq_users_email`
constraint can rule it out atomically. A duplicate is a **409**.

A **409 does tell a caller that an address is registered.** That is unavoidable
for a registration endpoint that answers synchronously, and it is recorded here
rather than hidden behind a 200 that creates nothing. Closing it needs the
verification-email flow.

### Why login checks the password before it checks anything else

```python
stored  = credentials.password_hash if credentials is not None else None
matched = verify_password(password, stored)      # always runs
if credentials is None or not matched: ...
```

A nonexistent account, an account with no password, a wrong password and a
disabled account all cost one bcrypt round and produce one error. Returning
early for an unknown address would answer in under a millisecond while a real
account spends ~230ms — an enumeration oracle that identical response bodies do
nothing to close. `verify_password` performs the throwaway round itself when
handed no hash, so a caller cannot forget to.

`matched` is computed *before* the branch deliberately: `credentials is None or
not verify_password(...)` short-circuits, skipping the round for exactly the
case that needs it.

### Why a failed login looks like a failed token

Both raise `unauthenticated()` — the same helper, so the detail string and the
`WWW-Authenticate` header cannot drift apart:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer

{"detail": "Could not validate credentials"}
```

### Reading a credential

`UserRepository.get_credentials_by_email` is the only method that returns
`password_hash`, and it returns `UserCredentials` — never `User`. Putting the
hash on `User` to make login convenient would undo the boundary: `User` is what
every read path returns.

It is **not** filtered on `is_active`, because skipping disabled accounts would
make them answer in a different time from live ones. The service checks
`is_active` after the password comparison.

## What is still missing

No RBAC and no 403 (S2.5) — `require_role` is still a stub. No password reset,
no email verification, no MFA, no account recovery. No refresh tokens, and that
one is by design rather than by schedule (principles.md §6).
