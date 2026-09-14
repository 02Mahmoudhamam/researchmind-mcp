# Security Policy

## Reporting a vulnerability

**Please report privately. Do not open a public issue.**

Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on this repository. It creates a private
advisory visible only to maintainers.

### Never include a live credential in a report

If you discovered an exposed key, token or password:

1. **Rotate or revoke it first.** Removing a secret from a repository does not
   un-disclose it — assume anything committed is compromised.
2. Report the **exposure** — where it was, how you found it, what it granted.
3. Do **not** paste the value into an issue, advisory, pull request or log
   excerpt. Redact it.

### What helps

- What an attacker gains, and what access they need to start.
- Reproduction steps, with credentials and tokens redacted.
- Affected commit or version.
- Whether you believe it is being exploited.

## Supported versions

Pre-alpha; no released version is supported. Only the current `main` receives
fixes. The project is **not production-ready** and must not be exposed to an
untrusted network.

## Known security state

This repository is transparent about its current posture rather than silent.
As of the foundation baseline, verified by execution:

| Issue | Status |
|---|---|
| ~~**Authentication fails open** — any non-empty bearer token is accepted and resolves to a null user~~ | **Resolved — M2/S2.2.** `get_current_user` returns a `Principal` or raises. Every protected route rejects a forged, malformed, expired, tampered, wrong-secret or unresolvable token with **401**, proven by a regression test over all nine routes and by mutation verification. Tokens for deleted or deactivated users are rejected on their next request |
| ~~No authorisation — RBAC is defined but never enforced~~ | **Resolved — M2/S2.5.** Every protected route is authorised by permission through `RBACPolicy`, using the role PostgreSQL holds *now* — never the token's role claim — so a demotion takes effect on the next request. 401 (unauthenticated) and 403 (not permitted) are distinct and tested on all nine routes. Ownership is a separate layer: no role, including administrator, can read or delete another user's documents |
| Registration reveals whether an email is registered | **Accepted, M2/S2.4.** `POST /auth/register` answers **409** for a taken address, so it is an enumeration oracle. Unavoidable for a registration endpoint that answers synchronously; closing it needs the verification-email flow, which is not yet scheduled. **Login is not an oracle** — unknown address, wrong password, no password set and disabled account produce byte-identical 401s *and* the same bcrypt cost |
| Email uniqueness is case-sensitive | **Open.** `Alice@example.com` and `alice@example.com` are two accounts, and signing in with the wrong case fails. The fix is a `LOWER(email)` unique index plus normalisation at the boundary, which needs a migration; deferred rather than done as a side effect of M2/S2.4 |
| No tenant isolation — no relational store to check ownership against | Open — M1/M4 |
| ~~Default `changeme` secrets with no fail-fast~~ | **Resolved — M2/S2.1.** The application refuses to start on a placeholder, empty or under-32-character secret outside `APP_ENV=development` |
| ~~Unrestricted CORS (`allow_origins=["*"]`)~~ | **Resolved — M2/S2.1.** Explicit origin list from `CORS_ORIGINS`, enumerated methods and headers, credentials disabled |
| No rate limiting | Open — M9 |
| ~~No upload validation~~ | **Resolved — M3/S3.1.** Uploads are validated before anything is stored: magic-byte sniffing (the client `Content-Type` is ignored), a size cap, a page cap, and refusal of password-protected PDFs. The client filename never forms a path — storage keys are `{user_id}/{sha256}.pdf`, and the backend refuses any other shape. The owner is taken only from the authenticated principal |
| Oversized uploads are received before they are refused | **Open — M9.** FastAPI spools a multipart body to disk before the handler runs; the application refuses anything over `MAX_UPLOAD_BYTES` and never loads more than that into memory, but a transport-level body limit (proxy or middleware) does not exist yet |

These are tracked work items in
[docs/roadmap/MILESTONES.md](docs/roadmap/MILESTONES.md), not undiscovered
risks. Reports of *additional* issues are very welcome.

## Known dependency advisories

`npm audit` reports **5 advisories (1 critical, 4 high)** against the frontend:
`next`, and `postcss` / `glob` / `eslint-config-next` / `@next/eslint-plugin-next`
transitively. Every available fix is a **major** upgrade — `next` 14 → 16, which
also pulls React 19 and ESLint 9 flat config. They are therefore **accepted, not
yet fixed**, and this section exists so that is a decision on the record rather
than an omission.

**Why they are not currently reachable.** The advisories against `next` are
concentrated in features this frontend does not use. Verified by inspection at
Sprint M0/S0.5, all counts zero:

| Advisory area | Used here? |
|---|---|
| Image Optimizer (incl. the AVIF RCE, `GHSA-2xp9-vwfh-vxw4`) | No `next/image` import, no `<Image>`, no `images` config, no `public/` |
| Server Actions / Server Components DoS | No `"use server"`; all routes prerender static |
| Middleware / proxy bypass, rewrites SSRF | No `middleware.ts`, no `rewrites`/`redirects` |
| i18n Pages-Router bypass | No `i18n` config; App Router only |
| CSP-nonce XSS, `beforeInteractive` XSS | No nonce use, no `beforeInteractive` scripts |
| Windows-hosted RCE (`GHSA-p293-qw3h-jr36`, CVSS 9.0) | Image runs on `node:20-alpine` (Linux) |

`postcss` and `glob` are **build-time** dependencies. They process this
repository's own CSS and file globs during `npm run build`; they never see
attacker-controlled input at runtime.

**What would change this.** The moment the UI stops being placeholders — an
image, a Server Action, middleware, a rewrite, or a real deployment — this
analysis expires. Re-run it, do not inherit it.

> This is a *reachability* argument, not a claim that the versions are safe. The
> repository is pre-alpha and must not be exposed to an untrusted network
> (see **Supported versions** above), which is what actually bounds the risk today.

**Policy.** `.github/dependabot.yml` deliberately does **not** ignore `next`
majors, so the upgrade PR stays visible and has to be decided on. React and the
base-image majors *are* ignored, because those are pinned architectural
decisions rather than dependency drift.

## Security model

Invariants — fail-closed authentication, token-only identity, two-layer tenant
isolation, untrusted-document handling, server-resolved citations, secret and
logging rules — are documented in
[docs/security/principles.md](docs/security/principles.md).

## Automated tooling

Secret scanning, push protection, dependency review and Dependabot are enabled.
They detect known credential formats and known-vulnerable versions.

They did not catch the fail-open authentication bug — it sat in `main` through
M0 and M1, and what found it was reading the code, not scanning it. Nor would
they catch a missing tenant filter, or an agent reporting success without doing
work. Those are caught by the release-gate test suites, and nothing else.
Tooling supplements security engineering; it does not replace it.
