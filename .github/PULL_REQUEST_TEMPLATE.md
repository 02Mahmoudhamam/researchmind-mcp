## Summary

<!-- What changes, and why. One or two sentences. -->

## Related Sprint / Milestone

<!-- e.g. M2/S2.2 — or "repository maintenance". Link the issue. -->

Closes #

## Changes Made

<!-- Bullet the substantive changes. Not a file list — the diff is the file list. -->

## Architecture Impact

<!-- New/changed interface, dependency, or data flow? Does it need an ADR?
     Does it change an existing ADR's assumptions? "None" is a valid answer. -->

## Security Impact

<!-- Authentication, authorisation, tenant isolation, secrets, untrusted input,
     or dependencies. "None" is a valid answer — but consider it before writing it. -->

## Tests Performed

<!-- What you ran and what it proved. Paste relevant output. -->

## Acceptance Criteria

<!-- Copy from the sprint/issue and tick what is genuinely met. -->

- [ ]
- [ ]

## Evidence

<!-- Screenshots, terminal output, CI links. Required for milestone PRs. -->

---

## Checklist

- [ ] **No placeholder completion** — no `pass`, `...`, `return None`, `return {}`,
      or `success=True` standing in for work that did not happen
- [ ] **No unresolved `TODO`** introduced in new code
- [ ] **Tests pass locally** and cover the new behaviour
- [ ] **Tests can actually fail** — they would break if the behaviour were removed
- [ ] **CI is green**
- [ ] **No secrets committed** — no keys, tokens or `.env` files
- [ ] **Security implications reviewed** (identity, ownership, untrusted input)
- [ ] **Fail-closed preserved** — no new permissive fallback path
- [ ] **Documentation updated** where behaviour changed
- [ ] **ADR added** if a structurally significant decision was made
- [ ] **Migration included** if the schema changed
- [ ] **Commits follow Conventional Commits** and are logically separated

<!-- Milestone PRs only -->
- [ ] Every milestone Exit Criterion verified, with evidence above
- [ ] Full test suite green
- [ ] Ready to tag
