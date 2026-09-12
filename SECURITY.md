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
| **Authentication fails open** — any non-empty bearer token is accepted and resolves to a null user | Open — fixed in Milestone M2 |
| No authorisation — RBAC is defined but never enforced | Open — M2 |
| No tenant isolation — no relational store to check ownership against | Open — M1/M4 |
| Default `changeme` secrets with no fail-fast | Open — M2 |
| Unrestricted CORS (`allow_origins=["*"]`) | Open — M2 |
| No rate limiting | Open — M9 |
| No upload validation | Open — M3 |

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

They would not have caught the fail-open authentication bug, a missing tenant
filter, or an agent reporting success without doing work. Those are caught by
the release-gate test suites, and nothing else. Tooling supplements security
engineering; it does not replace it.
