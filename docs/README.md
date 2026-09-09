# ResearchMind MCP — Documentation

Documentation index. Every document here has a defined purpose; there are no
placeholder pages.

## Where to start

| If you want to… | Read |
|---|---|
| Understand what the system is for | [roadmap/PROJECT_VISION.md](roadmap/PROJECT_VISION.md) |
| Know what actually works today | [architecture/AUDIT-2026-09.md](architecture/AUDIT-2026-09.md) |
| Understand the target architecture | [architecture/ARCHITECTURE.md](architecture/ARCHITECTURE.md) |
| Know why a design choice was made | [adr/](adr/) |
| Know what is being built next | [roadmap/MILESTONES.md](roadmap/MILESTONES.md) |
| Contribute code | [development/workflow.md](development/workflow.md) |
| Configure the project | [development/environment.md](development/environment.md) |
| Understand the security model | [security/principles.md](security/principles.md) |

## Directory map

| Path | Contents |
|---|---|
| `adr/` | Architecture Decision Records — the authoritative record of *why* |
| `architecture/` | System design and the verified audit of current state |
| `roadmap/` | Milestone plan, sprint detail, product vision |
| `development/` | Workflow, environment configuration, testing strategy |
| `security/` | Security principles and non-negotiable invariants |
| `operations/` | Deployment and runbooks — created at Milestone 10 |

## Document status conventions

Documents carry an explicit banner when they are not a description of current
behaviour:

- **TARGET STATE** — describes the system as intended, not as built.
- **SUPERSEDED** — retained for provenance; a newer document is authoritative.

A document with no banner describes the repository as it actually is.

## A note on trust

The original design documents in this repository described unimplemented
behaviour in the present tense. That is why banners exist, and why
[architecture/AUDIT-2026-09.md](architecture/AUDIT-2026-09.md) — which is
evidence-based and cites `file:line` — is the reference for current state.
When a document and the code disagree, the code wins and the document is a bug.
