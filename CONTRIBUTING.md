# Contributing to ResearchMind MCP

## Before you start

The project is **pre-alpha and does not yet run**. Work follows an approved
milestone plan — [docs/roadmap/MILESTONES.md](docs/roadmap/MILESTONES.md) — and
milestone ordering is not arbitrary. M1 precedes M2 because tokens must bind to
persisted users; M2 precedes M4 because a tenant filter is only as trustworthy
as the identity feeding it.

Read first:

- [docs/development/workflow.md](docs/development/workflow.md) — branches, commits, releases
- [docs/development/testing.md](docs/development/testing.md) — what a real test looks like here
- [docs/development/migrations.md](docs/development/migrations.md) — writing and verifying a schema change
- [docs/development/repositories.md](docs/development/repositories.md) — the data-access boundary and how ownership is enforced
- [docs/development/transactions.md](docs/development/transactions.md) — the service layer and who owns commit
- [docs/development/authentication.md](docs/development/authentication.md) — how a request acquires an identity, and why a route cannot skip it
- [docs/development/uploads.md](docs/development/uploads.md) — upload validation, content-addressed storage, duplicates, and the ingestion hand-off
- [docs/development/ingestion.md](docs/development/ingestion.md) — the ARQ worker: status lifecycle, failure reasons, retries, idempotency, the stale-claim reaper and the recovery sweep
- [docs/development/pdf-extraction.md](docs/development/pdf-extraction.md) — PDF text extraction: stored format, reading order, cleaning, failures and the time budget
- [docs/adr/](docs/adr/) — why the system is shaped this way
- [docs/security/principles.md](docs/security/principles.md) — non-negotiable invariants

## Setup

```bash
cp .env.example .env                          # set ANTHROPIC_API_KEY
cd frontend && cp .env.example .env.local && cd ..
./scripts/check-hygiene.sh
```

`docker compose up` does not work yet; Milestone M0 is what fixes it.

## Workflow

```
git switch -c sprint/mX-sYY-short-name    # from the milestone branch
# implement, committing logical units as you go
poetry run pytest && ./scripts/check-hygiene.sh
git push -u origin sprint/mX-sYY-short-name
# open a PR into milestone/mX
```

Never push to `main`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, with `Sprint: MX/SYY` in the footer.

Types: `feat` `fix` `refactor` `perf` `docs` `test` `build` `ci` `chore` `sec`
Scopes: `api` `mcp` `agents` `rag` `db` `vector` `auth` `jobs` `frontend`
`infra` `docs` `repo`

The body explains **why**; the diff already shows what. One logical change per
commit. Never `update project`, `fix everything`, `wip`.

## Standards

**No placeholder completion.** None of the following is an implementation:

```python
pass
...
return None
return {}
success=True          # without the work actually having happened
```

If something cannot be finished in a sprint, say so in the PR and leave it
unmerged. Do not merge a shell.

**Fail closed.** If identity, authorisation or ownership cannot be established,
deny. No permissive fallbacks.

**Identity comes only from a validated token** — never from a request body,
query parameter or tool argument.

**Every retrieval enforces the tenant filter server-side.** It is a required
parameter, not an optional one.

**A test must be able to fail.** A test that passes because the code does
nothing is worse than no test, and two were deleted from this repository for
exactly that reason.

**Never commit a secret.** If you do: rotate it first, then clean history.

## Pull requests

Fill the template honestly — its checklist is the review contract. A PR should
be one sprint or one focused change, with tests, and with documentation updated
where behaviour changed.

## Reporting bugs and vulnerabilities

Bugs: open an issue with the template. Vulnerabilities: **private reporting
only** — see [SECURITY.md](SECURITY.md).
