# ADR-0003 — PostgreSQL as System of Record, Qdrant as index only

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0008, Milestones M1, M4; security/principles.md

## Context

The repository has no relational database, no ORM and no migrations.
`backend/api/dependencies/database.py` yields only Qdrant and Redis clients,
and `shared/interfaces/repository.py:BaseRepository` has **zero
implementations**.

This single omission blocks four things simultaneously: user persistence,
document listing, ownership checks, and tenant isolation. A JWT cannot be bound
to a user that is not stored anywhere, and an ownership filter has nothing to
filter against.

Qdrant cannot fill the gap. It is an approximate-nearest-neighbour index with
no relational integrity, no transactions and no foreign keys. Using it as the
application database would make ownership a payload convention rather than a
constraint.

## Decision

**PostgreSQL 16 is the sole authority for what exists and who owns it. Qdrant
is an index, never a source of truth.**

1. SQLAlchemy 2.x async + `asyncpg` + Alembic, with migrations from the first
   commit.
2. ORM models for `User`, `Document`, `DocumentChunk`. The chunk table is not
   redundant with Qdrant: it is what makes reconciliation, provenance and
   targeted re-indexing possible.
3. Implement `BaseRepository` as `UserRepository`, `DocumentRepository`,
   `ChunkRepository`, finally giving the existing ABC its implementations.
4. Denormalise `user_id` and `document_id` into every Qdrant payload, with
   **keyword payload indexes on both**. Without an index, filtering degrades to
   a scan as the corpus grows.
5. **The isolation invariant.** Retrieval is filtered by `user_id` in Qdrant
   (the *fast* path) **and** every returned chunk id is re-validated against
   PostgreSQL ownership before any content enters a prompt (the *correct*
   path).
6. **Deletion order is fixed:** soft-delete in PostgreSQL (committed first) →
   delete vectors in Qdrant → finalise. Never the reverse.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| pgvector, drop Qdrant | One database for rows and vectors | Attractive — removes a service and the dual-write problem. Rejected because Qdrant integration (client, config, interface) already exists and works, and payload filtering with keyword indexes is well suited to the dominant document-scoped query. Revisit if operational burden proves real. |
| Qdrant payloads as the only store | No relational database | Ownership becomes convention, not constraint. No transactions, no referential integrity, no uniqueness on user email. |
| SQLite | Simpler single-node store | No async driver story comparable to asyncpg; a migration to Postgres later would land exactly when the system is least able to absorb it. |

## Consequences

**Good.** Ownership becomes a database constraint. The dual-write failure mode
becomes *safe*: if a delete succeeds in Postgres but fails in Qdrant, orphaned
vectors can still be retrieved but are dropped at validation — the system
degrades to "fewer results", never "leaked another user's manuscript".

**Bad.** Two stores to operate, back up and keep consistent. Every retrieval
pays one extra Postgres round-trip. Adds a service to compose.

**Neutral.** Alembic must be maintained from the start, which is cheaper than
retrofitting migrations onto a live schema.
