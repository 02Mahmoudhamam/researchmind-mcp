# Security Principles

Non-negotiable invariants. These are **requirements, not architectural
trade-offs** — which is why they are recorded here rather than as ADRs. An ADR
implies a decision someone might revisit; nothing on this page is revisitable
without an explicit, documented security exception.

## 1. Fail closed

If identity cannot be validated, **deny**.
If authorisation cannot be established, **deny**.
If ownership cannot be verified, **deny**.

There is no fallback path, no permissive default, and no "allow while we
investigate".

### The defect this exists to prevent

The repository baseline shipped an authentication bypass, verified by
execution:

```
no Authorization header      -> 403 Not authenticated
Bearer totally-fake-not-a-jwt -> 200   user is None     ← accepted
```

`HTTPBearer` correctly rejected a *missing* header, which made the bypass
invisible in manual testing, while `get_current_user` verified nothing and
returned `None`. Every protected endpoint accepted forged credentials.

**Closed in Sprint M2/S2.2**, verified by execution on the same nine routes:

```
no Authorization header       -> 401 Could not validate credentials
Bearer totally-fake-not-a-jwt -> 401 Could not validate credentials
```

The rules below are now enforced by
[`tests/integration/test_authentication.py`](../../tests/integration/test_authentication.py)
and by structural tests in `tests/unit/test_architecture.py`. See
[docs/development/authentication.md](../development/authentication.md) for the
resulting contract.

### Rules

- `get_current_user` returns a `Principal` or **raises**. It can never return
  `None`, `Optional[User]`, or a falsy value. Enforced by type and by test.
- JWT decode pins the algorithm explicitly (`algorithms=["HS256"]`). It is
  **never** inferred from the token header — that is algorithm confusion.
- `exp` is **required**, not optional. A token without an expiry is invalid.
- A structurally valid token whose subject does not resolve to an **active**
  user is rejected.
- Verification failures raise typed errors. They never return a sentinel a
  caller might treat as success.

## 2. Identity comes only from a verified token

**Never** from a request body, query parameter, header, or MCP tool argument.

No service method accepts a caller-supplied `user_id`. Every service method
takes an authenticated `Principal` constructed by the shared resolver.

REST and MCP use the **same** identity resolver, so MCP cannot become a path
around REST authentication.

## 3. Tenant isolation is enforced twice

> **PostgreSQL is the sole authority for what exists and who owns it. Qdrant is
> an index, never a source of truth. Every retrieval result is validated
> against PostgreSQL before any content reaches the LLM.**

- **Fast path** — Qdrant search filters on `user_id`, with a keyword payload
  index. The filter is a **required parameter of the repository signature**, not
  an optional `filters` dict entry. The safe path must be the only path.
- **Correct path** — returned chunk ids are re-fetched from PostgreSQL and any
  not owned by the principal are dropped.

This makes the dual-write failure mode *safe*. If a delete succeeds in Postgres
but fails in Qdrant, orphaned vectors can still be retrieved — but they are
dropped at validation. The system degrades to "fewer results", never "leaked
another user's manuscript".

**Deletion order is fixed:** soft-delete in PostgreSQL (committed first) →
delete vectors in Qdrant → finalise. Never the reverse.

## 4. Untrusted input

**Uploaded documents are data, never instructions.** Document text reaching a
prompt is delimited and labelled untrusted, under an instruction-hierarchy
system prompt. An adversarial PDF is part of the test corpus.

**Filenames never form filesystem paths.** Storage is content-addressed as
`{user_id}/{sha256}.pdf`; the client filename is a database column only. This
makes path traversal structurally impossible rather than defended against.
See [ADR-0008](../adr/0008-local-content-addressed-object-storage.md).

**Uploads are validated before they are stored** — magic-byte MIME sniffing
(never the client `Content-Type`), size cap, page cap, encrypted-PDF rejection.

## 5. Citations are resolved server-side

Citation metadata — document, page, section — is resolved from PostgreSQL,
never taken from model output, so the model cannot fabricate a source that
looks legitimate. Markers that do not resolve to a supplied chunk are stripped
and counted as an integrity violation.

**Zero retrieval means no LLM call.** The service returns an explicit "no
relevant sources" answer. This removes the highest-probability hallucination
path entirely, and costs one `if`.

## 6. Secrets

- Never committed; never baked into an image; placeholders only in examples.
- The application **refuses to start** with default `changeme` secrets outside
  `APP_ENV=development`.
- Access tokens only, 60-minute TTL, no refresh token in the MVP. A refresh
  system done properly needs rotation, reuse detection and revocation storage;
  done improperly it is worse than none. There is **no server-side revocation**,
  so a stolen token is valid until expiry — stated plainly rather than hidden.
- If a secret is exposed: **rotate first**, then clean history.

## 6a. Passwords

Stored as a **bcrypt** digest and nothing else. Plaintext exists in the process
for the duration of one function call in
[`backend/security/passwords.py`](../../backend/security/passwords.py) and is
never written, logged, or placed in an exception message.

- **Minimum 12 characters**, and **no composition rules**. Requiring a capital,
  a digit and a symbol produces `Password1!` — it narrows the space attackers
  search far more than it widens the one they theoretically must. NIST SP
  800-63B has advised against composition rules since 2017.
- **Maximum 72 bytes**, measured in UTF-8 and not in characters. bcrypt reads
  at most 72 bytes; 30 CJK characters are 90.
- **Rejected, never truncated.** bcrypt 4.3.0 does *not* raise on a longer
  password — it silently ignores the tail, so a 97-byte password verifies
  against a hash built from its first 72 bytes. Measured, not assumed. The
  explicit check is the only thing preventing two different passwords from
  hashing identically.
- **NFKC normalisation** on hashing *and* on verification. Normalising only on
  the way in means a user whose keyboard emits a compatibility form enrols once
  and can never sign in again.
- The policy applies where a password is **chosen**, not where one is
  **presented**. Validating a sign-in attempt would lock out a user whose
  password predates a tightening of the rules, and would answer "too short"
  where the only safe answer is "wrong".
- `password_hash` is **nullable**, and every row is currently NULL. An account
  with no password cannot be signed into; `verify_password` returns False.
- The hash is storage-only. It is absent from the `User` API contract and from
  `Principal`, and never appears in a response.

## 7. Logging

Never log tokens, passwords, API keys, or document content. Redaction is
applied at the structlog processor level, not per call site, and is verified by
a test asserting no secret or document text appears in captured output.

Error responses carry no internal detail — no stack traces, no query text, no
file paths.

## 8. Tooling is not a substitute

GitHub secret scanning, push protection and Dependabot are enabled, and they
are worth having. They detect *known credential formats* and *known vulnerable
versions*.

They would not have caught the fail-open authentication bug, the missing tenant
filter, or an agent reporting success without doing work. Those are caught by
the release-gate test suites in
[../development/testing.md](../development/testing.md), and nothing else.

## Reporting a vulnerability

See [SECURITY.md](../../SECURITY.md). Report privately. **Never include a live
credential in a report** — rotate it and describe the exposure instead.
