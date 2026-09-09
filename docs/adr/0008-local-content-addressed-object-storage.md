# ADR-0008 — Local content-addressed object storage for uploads

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0003, Milestone M3; security/principles.md

## Context

`DocumentService.upload_and_process` must persist uploaded bytes somewhere. The
repository has no storage abstraction, `docker-compose.yml` mounts
`./uploads:/app/uploads` for a directory that **does not exist**, and the
`Document` model has **no storage pointer** — no `storage_key`, `content_hash`,
`size_bytes` or `mime_type`. A complete upload implementation would have
nowhere to record where the file went.

Upload handling is also where path traversal lives. `UploadFile.filename` is
attacker-controlled, and a naive `Path(upload_dir) / file.filename` permits
`../../` escapes.

The deployment target is single-node.

## Decision

**Local volume storage behind a `Storage` protocol, content-addressed.**

1. Define `Storage` in `shared/interfaces/` with `put` / `get` / `delete`.
2. `LocalStorage` writes to `{user_id}/{sha256}.pdf`.
3. **The client-supplied filename never forms part of a filesystem path.** It is
   persisted as a database column only. This makes path traversal structurally
   impossible rather than defended against.
4. Add `storage_key`, `content_hash`, `size_bytes`, `mime_type`, `page_count`
   to the `Document` model.
5. Content addressing gives deduplication for free.
6. Backup is a volume snapshot plus `pg_dump`.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| MinIO in compose, S3 API | Self-hosted object store | Adds a service, credentials and a health dependency to buy an abstraction the `Storage` protocol already provides. Warranted when multi-node arrives, not before. |
| Direct cloud S3 | Managed object storage | Adds a cloud dependency and credentials to a stack whose goal is one secret; contradicts local-first development. |
| Database BLOBs | Store bytes in Postgres | Bloats the database and its backups; poor fit for multi-megabyte PDFs. |
| Filename-derived paths | `uploads/{filename}` | Directly reintroduces path traversal and collisions. |

## Consequences

**Good.** Path traversal is eliminated by design. Deduplication is free.
Zero additional services. The protocol is the seam for a future S3 backend.

**Bad.** Horizontal scaling requires a shared filesystem or a new
implementation. Backup is a second procedure alongside `pg_dump`. Orphaned
blobs are possible if a database delete succeeds and the file delete fails —
acceptable, since the failure wastes disk rather than leaking data.

**Neutral.** Content addressing means the original filename is display metadata
only.
