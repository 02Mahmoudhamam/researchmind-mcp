# Development Workflow

How work moves from a sprint definition to a tagged, validated state on `main`.

## Branch strategy

```
main  ────●────────────────●────────────────●──────────────►
       v0.0.1           v0.1.0           v0.2.0
      foundation        M0 done          M1+M2 done
          │                ▲                ▲
          │   milestone/m0 │   milestone/m1 │
          └──●──●──●───────┘   └──●──●──────┘
             │  │  │             │  │
      sprint/m0-s01           sprint/m1-s11
      sprint/m0-s02           sprint/m1-s12
```

| Branch | Purpose | Lifetime |
|---|---|---|
| `main` | Stable. **Only validated milestone states.** Protected. | Permanent |
| `milestone/mX-name` | Integration point for one milestone's sprints | Until the milestone merges |
| `sprint/mX-sYY-name` | One sprint's work | Until its PR merges |
| `fix/name`, `docs/name`, `chore/name` | Small standalone changes | Until merged |

**No `develop` branch.** There is no release train to stabilise, so a
permanently-diverging integration branch would add a merge step for no benefit.
This is not GitFlow: no `develop`, no `release/*`, no `hotfix/*`.

### Why milestone branches exist

Merging each *sprint* straight to `main` would put half-finished milestones
there. For **M2 that is a security problem**: M2 spans two sprints, and merging
only the first leaves `main` able to mint tokens while `get_current_user` still
fails open. The milestone branch exists so that **security-relevant milestones
land atomically**.

## Sprint cycle

```
1. git switch milestone/mX && git pull
2. git switch -c sprint/mX-sYY-short-name
3. Implement — commit logical units as you go, not one blob at the end
4. Run the sprint's required tests locally; they must pass
5. git push -u origin sprint/mX-sYY-short-name
6. Open a PR into milestone/mX and fill the template honestly
7. CI green + self-review against the sprint Definition of Done
8. Squash-merge, delete the branch
```

Squash-merging sprint → milestone keeps the milestone branch readable (one
commit per sprint) while the PR preserves the detailed work.

## Milestone cycle

```
1. All sprints merged into milestone/mX
2. Run milestone validation: full test suite + the milestone Exit Criteria
3. Verify every Definition-of-Done item, with recorded evidence
4. Open a PR milestone/mX → main, titled with the milestone outcome
5. The PR body lists Exit Criteria with evidence — this is the audit record
6. CI green on the full suite
7. MERGE COMMIT (never squash) — preserves per-sprint history on main
8. git tag -a vX.Y.0 -m "Milestone X: <outcome>"
9. git push origin main --follow-tags
10. Delete the milestone branch
```

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
