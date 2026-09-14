# ADR-0010 — Per-owner idempotent upload over content-addressed storage

- **Status:** Proposed — implemented in Milestone M3/S3.1; supersede if rejected
- **Date:** 2026-09-14
- **Deciders:** Implementation of M3/S3.1, pending owner review
- **Related:** ADR-0003, ADR-0008, ADR-0009; Milestone M3/S3.1

## Context

ADR-0008 stores an upload at `{user_id}/{sha256}.pdf` and says content addressing
"gives deduplication for free". That settles what happens to the **bytes**: the
same owner uploading the same content twice produces one blob, at one key.

No ADR, roadmap entry or test says what happens to the **database row**. Three
readings are possible — a second row pointing at the same blob, the existing row
returned, or the upload refused — and they are not interchangeable, because the
storage key forces rows for the same owner and content to *share* a blob:

- A second row makes the blob shared between two live documents. ADR-0003's
  deletion order ends in a finalise step that removes the blob; with shared
  blobs, finalising one document deletes the file the other still points at,
  unless deletion reference-counts. Both rows would also be ingested, so the
  same passages would be embedded twice and returned twice by retrieval.
- Refusing the upload gives a client nothing to act on: it still needs the id of
  the document it already has.
- A plain "look for an existing row, then insert" is a race, for exactly the
  reason S2.4 did not pre-check email uniqueness.

Deduplication **across** owners is not on the table. The key contains the
owner id, which keeps every tenant's blobs separate: deleting one user's
document can never remove another user's file, and one user cannot learn that
another has uploaded a given file.

## Decision

**An upload is idempotent per owner and per content.**

1. If the caller already has a **live** (not soft-deleted) document whose
   `content_hash` matches, the upload returns that document with **200**. No
   row is inserted, nothing is written to storage, and no ingestion job is
   enqueued.
2. Otherwise a new document is created and the response is **202**, as
   ADR-0009 specifies.
3. Uniqueness is enforced by PostgreSQL, not by the lookup: a **partial unique
   index on `(user_id, content_hash) WHERE deleted_at IS NULL`**. Two concurrent
   uploads of the same file by the same owner cannot both insert; the loser's
   IntegrityError is caught and it returns the winner's document.
4. A soft-deleted document does not count. Uploading the same file after
   deleting it creates a new document, which may reuse the blob still on disk.
5. The existing document is returned **as it is** — its original filename, and
   its current status, including a terminal failure. Re-uploading is not a
   retry mechanism; deleting and uploading again is.
6. Different owners uploading identical content get independent documents and
   independent blobs.

The consequence for storage cleanup, required of the implementation: **a blob
is only deleted by the request that created it**, and never when the insert
failed because an equivalent live row already exists.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| New row per upload | Every upload creates a document, sharing the blob | Deletion must reference-count blobs or break a sibling document; identical content is ingested, embedded and retrieved twice. |
| Reject duplicates | 409 when the owner already has the content | The client still needs the existing id to do anything; a retry after a dropped connection becomes an error instead of a success. |
| Application-level check only | Look up, then insert, no constraint | Concurrent uploads both pass the check. The constraint is what makes the rule true. |
| Cross-owner deduplication | One blob per content hash, globally | Couples tenants' storage lifetimes and makes "someone else has this file" observable. Rejected by ADR-0008's key design already. |

## Consequences

**Good.** A client that lost the response can retry safely. Identical content is
stored, ingested and embedded once per owner. Retrieval never returns the same
passage twice from duplicate uploads. The rule is a database constraint.

**Bad.** A document whose ingestion failed cannot be retried by uploading again;
it has to be deleted first. The original filename of the *first* upload wins —
re-uploading under a new name does not rename. Finalising a soft delete (M4)
must still check whether a newer live row has reused the blob before removing it.

**Neutral.** 200 and 202 now mean different things on the same route: "you
already have this" and "accepted for ingestion".

## References

- ADR-0008 §2, §5 — storage key and "deduplication for free"
- ADR-0009 §2 — 202 with the document id
- `docs/development/authentication.md` — why uniqueness belongs to a constraint
