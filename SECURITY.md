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
