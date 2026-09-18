# Development Workflow

How work moves from a sprint definition to a validated state on `main`.

> **Corrected 2026-09-18 (M4/S4.2).** This page described a three-tier model —
> `sprint/*` → `milestone/*` → `main`, squash-merged — that the project has
> never used. Twenty sprints merged directly to `main` with `--no-ff`, no
> `milestone/*` branch was ever created, and no sprint history was squashed.
> `docs/README.md` says "when a document and the code disagree, the code wins
> and the document is a bug", so the page was the bug. What follows is what the
> history actually shows.

## Branch strategy

```
main  ────●──────●──────●──────●──────●──────●──────►
          ▲      ▲      ▲      ▲      ▲      ▲
          │      │      │      │      │      │   --no-ff merge commits,
          │      │      │      │      │      │   one per sprint
   sprint/m3-s31 │      │  sprint/m3-s35      │
          sprint/m3-s32 │         sprint/m4-s41
                 sprint/m3-s33         sprint/m4-s42
```

| Branch | Purpose | Lifetime |
|---|---|---|
| `main` | Stable. Every sprint lands here validated. Protected. | Permanent |
| `sprint/mX-sYY-name` | One sprint's work | **Retained after merge** unless deletion is explicitly authorised |
| `fix/name`, `docs/name`, `chore/name` | Small standalone changes | Until merged |

**No `develop` branch, and no `milestone/*` branch.** There is no release train
to stabilise. This is not GitFlow: no `develop`, no `release/*`, no `hotfix/*`.

### Why there is no milestone branch

The original argument was that a half-finished milestone on `main` is a security
problem — M2 merging token issuance before `get_current_user` was fail-closed.
In practice each sprint has been made **independently safe to merge**: a sprint
that would leave `main` in a worse state than it found it is not finished. M2's
five sprints each landed with authentication no weaker than before, which is
the property the milestone branch was meant to buy, obtained more cheaply.

The cost is that `main` shows in-progress milestones. That is why every
milestone's state is recorded honestly in `docs/roadmap/MILESTONES.md` and
`CLAUDE.md` rather than inferred from the branch graph.

## Sprint cycle

```
1. git switch main && git pull --ff-only
2. git switch -c sprint/mX-sYY-short-name
3. Implement — commit logical units as you go, not one blob at the end
4. Run the sprint's required gates locally; they must pass
5. git push -u origin sprint/mX-sYY-short-name
6. Re-verify main has not moved; run the gates again if it has
7. git switch main && git merge --no-ff sprint/mX-sYY-short-name
8. Run the important gates AGAIN on main after the merge
9. git push origin main          # never --force
10. Keep the branch. Delete only when explicitly authorised
```

**Never squash a sprint.** The per-sprint commits are the audit record of how a
decision was reached, including the tests that failed first. The `--no-ff`
merge commit carries the sprint's summary and evidence; the commits under it
carry the working.

**Never rebase, amend or force-push anything that has been pushed.**

## Milestone cycle

```
1. Every sprint of the milestone merged to main, each with its gates green
2. Run milestone validation on main: full suite + the milestone Exit Criteria
3. Verify every Definition-of-Done item, with recorded evidence
4. Record the milestone's completion in MILESTONES.md and CLAUDE.md
5. git tag -a vX.Y.0 -m "Milestone X: <outcome>"   # only when authorised
6. git push origin main --follow-tags
```

**Tags are not automatic.** No milestone tag has been created in this
repository to date; `v0.0.1` marks the repository foundation. A tag is created
only when the owner asks for one, because a tag is a claim that a milestone's
exit criteria were met.

## Non-negotiable gates

- **Never push directly to `main`.**
- A milestone is never merged with a failing test, an unresolved TODO in new
  code, or an unmet exit criterion. Scaling scope down is a decision recorded in
  the PR, not quietly absorbed.
- **Tags mark validated states only.** If validation fails after tagging, the
  fix is a *new* tag — never a moved one.
- No placeholder completion. `pass`, `...`, `return None`, or `success=True`
  without execution do not constitute an implementation.

## Commit convention

[Conventional Commits 1.0.0](https://www.conventionalcommits.org/) with a
project scope vocabulary.

```
<type>(<scope>): <imperative summary, <= 72 chars, no trailing period>

<body — what changed and WHY. The diff already shows what. Wrap at 72.>

Sprint: MX/SYY
Refs: #NN

Co-Authored-By: ...
```

### Types

| Type | Use |
|---|---|
| `feat` | New user-facing or API capability |
| `fix` | Correcting broken behaviour |
| `refactor` | Restructuring with no behaviour change |
| `perf` | Performance only |
| `docs` | Documentation, ADRs, README |
| `test` | Tests only |
| `build` | Poetry, Docker, npm, dependencies |
| `ci` | GitHub Actions, Dependabot |
| `chore` | Repository hygiene, file moves |
| `sec` | Security-relevant change |

`sec` is a deliberate project extension, not part of the base specification. It
exists so that `git log --grep '^sec'` yields a complete security audit trail.

### Scopes

`api` · `mcp` · `agents` · `rag` · `db` · `vector` · `auth` · `jobs` ·
`frontend` · `infra` · `docs` · `repo`

### Rules

- One logical change per commit. Touching the auth layer *and* renaming a
  package is two commits.
- Breaking changes: `!` after the scope **and** a `BREAKING CHANGE:` footer.
- A `sec` commit body **must** state the impact and whether it was exploitable.
- Never `update project`, `fix everything`, `changes`, `wip`.

### Example

```
sec(auth): reject unverified bearer tokens

get_current_user returned None for any non-empty Authorization header,
so every protected endpoint accepted a forged token. HTTPBearer only
proved a header was present, which made the bypass invisible in manual
testing.

Identity is now resolved only from a signature-verified, unexpired JWT
whose subject resolves to an active user. The function raises 401 on
every failure path and can no longer return None.

Sprint: M2/S2.2
```

## Versioning

Semantic versioning, pre-1.0 — the API is not stable until v1.0.0. A tag marks
a **validated** state, never an aspirational one.

| Tag | Meaning |
|---|---|
| `v0.0.1` | Repository foundation. Engineering baseline, not a software release. |
| `v0.1.0` | M0 — build integrity: images build, imports resolve, CI green |
| `v0.2.0` | M1 + M2 — persistence and fail-closed authentication |
| `v0.3.0` | M3 — document ingestion works end to end |
| `v0.4.0` | M4 — tenant-isolated retrieval |
| `v0.5.0` | M5 — **first working end-to-end flow** |
| `v0.6.0` | M6 — MCP adapter |
| `v0.7.0` | M7 + M8 — evaluation harness and web client |
| `v0.9.0-rc.1` | M9 — MVP candidate |
| `v1.0.0` | M10 — release gate passed |

See [../roadmap/MILESTONES.md](../roadmap/MILESTONES.md).
