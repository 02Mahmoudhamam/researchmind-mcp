# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/), pre-1.0 — the API is
not stable until v1.0.0.

A version tag marks a **validated** state. Tags are never applied to broken or
incomplete work, and a tag is never moved: if validation fails afterwards, the
fix is a new tag.

## [Unreleased]

Next: **Milestone M0 — Build Integrity & Corrective Refactor**. Makes container
images build, resolves the `mcp` package shadowing, and gets the test suite
collecting. Target tag `v0.1.0`.

## [0.0.1] — 2026-09-09

**Repository foundation.** An engineering baseline, not a software release. The
application does not run at this tag.

### Added
- Git repository with `.gitignore` and `.dockerignore` committed before any
  source, excluding a 1.1 GB VS Code IntelliSense database that exceeds
  GitHub's 100 MB per-file limit.
- Nine Architecture Decision Records covering the approved architecture, with
  an index, template and stated process.
- Documentation structure: `architecture/`, `adr/`, `roadmap/`, `development/`,
  `security/`, each document with a defined purpose.
- Development workflow, environment reference, testing strategy and security
  principles.
- `frontend/.env.example`, separating frontend configuration from backend.
- GitHub Actions hygiene workflow, issue and pull request templates, labels,
  Dependabot, CODEOWNERS.
- `scripts/check-hygiene.sh`, runnable locally and in CI.
- Apache License 2.0.

### Changed
- README rewritten. The previous version claimed the project was
  "production-grade"; it now states the verified development status, including
  that authentication fails open.
- Documentation reorganised by purpose, with TARGET STATE banners on documents
  describing unimplemented behaviour.
- Infrastructure files moved to `infra/`; `health.py` moved beside the other
  routers.
- `.env.example` no longer breaks `Settings()`. It previously carried two
  frontend-only variables that caused `ValidationError: extra_forbidden`,
  breaking the first command of the documented Quick Start.

### Removed
- `devops/logging/logging_config.py` — dead duplicate of
  `shared/utils/logger.py`, zero references.
- A dangling `docs/API.md` link in the README, for a file that never existed.

### Security
- Verified: no secret has ever been committed. The repository had no prior Git
  history, so no remediation was required.
- Known-open issues documented in [SECURITY.md](SECURITY.md) rather than left
  implicit — including the fail-open authentication bug, scheduled for M2.

[Unreleased]: https://github.com/02Mahmoudhamam/researchmind-mcp/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/02Mahmoudhamam/researchmind-mcp/releases/tag/v0.0.1
