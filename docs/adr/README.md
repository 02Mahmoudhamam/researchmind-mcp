# Architecture Decision Records

An ADR records **why** a structurally significant decision was made, what was
rejected, and what it costs. It is written once, at the point of decision, and
is not edited afterwards — a decision that changes gets a *new* ADR that
supersedes the old one, so the reasoning history stays intact.

## Index

| ADR | Title | Status |
|---|---|---|
| [0000](0000-original-decision-record.md) | Original pre-approval decision record | Superseded |
| [0001](0001-modular-monolith-service-core.md) | Modular monolith with a shared Service Core | Accepted |
| [0002](0002-rest-and-mcp-as-sibling-adapters.md) | REST and MCP as sibling adapters over the Service Core | Accepted |
| [0003](0003-postgresql-system-of-record.md) | PostgreSQL as System of Record, Qdrant as index only | Accepted |
| [0004](0004-local-fastembed-embeddings.md) | Local FastEmbed embeddings (`bge-small-en-v1.5`) | Accepted |
| [0005](0005-provider-derived-embedding-dimension.md) | Embedding dimension derived from the provider abstraction | Accepted |
| [0006](0006-single-research-agent.md) | A single `ResearchAgent` replacing nine agent packages | Accepted |
| [0007](0007-defer-parent-document-retrieval.md) | Parent-document retrieval deferred pending evaluation | Accepted |
| [0008](0008-local-content-addressed-object-storage.md) | Local content-addressed object storage for uploads | Accepted |
| [0009](0009-arq-for-asynchronous-ingestion.md) | ARQ over Redis for asynchronous ingestion | Accepted |

## Statuses

`Proposed` · `Accepted` · `Superseded by ADR-NNNN` · `Deprecated`

## Writing a new ADR

1. Copy [template.md](template.md) to `NNNN-short-kebab-title.md`, next number.
2. Fill in every section. "Alternatives Considered" is not optional — an ADR
   with no rejected alternative is a note, not a decision.
3. Add a row to the index above.
4. Commit with `docs(adr): ...` and reference the sprint.

## What does *not* belong here

Security requirements are not ADRs. "Authentication must fail closed" and
"retrieval must be tenant-filtered" are non-negotiable invariants, not
trade-offs someone might revisit — they live in
[../security/principles.md](../security/principles.md), where they read as
requirements rather than choices.
