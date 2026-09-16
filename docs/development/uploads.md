# Document upload and storage

M3/S3.1. What happens between `POST /api/v1/documents/upload` and a `pending`
document. What the worker does with it afterwards is
[ingestion.md](ingestion.md) (M3/S3.2).

```
POST /api/v1/documents/upload   (multipart, field "file")
   │
   ├─ authentication            401   get_current_user
   ├─ authorisation             403   require_permission(DOCUMENT_WRITE)
   │
   ▼ DocumentService.upload_and_process(filename, stream, principal)
   │
   ├─ validate                  413 / 415 / 422   document_processing/validation.py
   ├─ deduplicate               200   this owner already has this content (ADR-0010)
   ├─ store                     503   Storage.put("{owner}/{sha256}.pdf")   (ADR-0008)
   ├─ record + commit           500   documents row, status "pending"
   └─ enqueue IngestionJob      503   after the commit, to ARQ over Redis    (ADR-0009)
   │
   ▼ 202  DocumentResponse
```

## The owner

`principal.user_id`, and nothing else. The route declares no field, query
parameter or header that could carry an owner, and anything a client sends
under such a name is never read. The same value becomes the storage directory,
the `documents.user_id` column and the job's `owner_id`. A test sends all four
possible impostors at once and checks all three places.

## Validation

`docs/security/principles.md` §4: validated **before** stored. Nothing is written
and no row exists for a refused upload.

| Rule | Refusal | Status |
|---|---|---|
| Size over `MAX_UPLOAD_BYTES` (default 50 MiB), stopped while reading | `too_large` | **413** |
| Does not start with `%PDF-` at offset 0 | `unsupported_type` | **415** |
| Empty | `empty` | 422 |
| Starts `%PDF-` but PyMuPDF cannot open it | `malformed` | 422 |
| Needs a password to open | `encrypted` | 422 |
| More than `MAX_PDF_PAGES` (default 500) | `too_many_pages` | 422 |
| No usable filename after sanitising | `invalid_filename` | 422 |

- **The client's `Content-Type` is never read.** A PDF labelled `text/plain` is
  accepted; text labelled `application/pdf` is 415.
- **"Encrypted" means cannot be read without a password.** A PDF with only an
  owner password (permission flags) opens and reads normally — measured — and
  is accepted. Publishers set these routinely.
- **Leading junk before `%PDF-` is refused**, although readers tolerate it: that
  tolerance is how polyglot files are made.
- PyMuPDF is opened for **structure only** — page count and whether a password
  is needed. No page is loaded and no text is read; two AST tests hold that line.
  Parsing is a later sprint and runs in the worker.

## Storage

`{STORAGE_ROOT}/{user_id}/{sha256}.pdf`, through the `Storage` protocol in
`shared/interfaces/storage.py`. `LocalStorage` is the only backend.

- **The filename never forms a path.** `document_storage_key(owner_id,
  content_hash)` has no parameter for one. `LocalStorage` refuses any key not
  matching that exact pattern, and refuses a resolved path outside the root.
- **Writes are atomic** — temporary file, fsync, hard link into place — so a
  crash leaves a stray `.upload-*.tmp`, never a truncated object at a real key.
- **`put` returns whether this call created the object.** `os.link` refuses an
  existing target atomically, so under a race exactly one caller is the creator.
- **SHA-256 over the bytes alone** — not the filename, owner or type.
- Per-owner directories: identical content uploaded by two users is two files.
  No cross-tenant deduplication, so no way to learn another user has a file.

The filename is stored as display metadata after sanitising: final path
component only (splitting on `/` and `\`), NFC, control/format/surrogate
characters removed. `../../etc/passwd.pdf` is stored as `passwd.pdf`.

## Duplicates — ADR-0010 (Proposed)

| Situation | Result |
|---|---|
| Same owner, same bytes, live document exists | **200**, that document. No write, no job. First filename kept. |
| Same owner, same bytes, previous one soft-deleted | **202**, new document; the file on disk is reused |
| Different owner, same bytes | **202**, independent document and file |
| Concurrent identical uploads by one owner | exactly one **202**; the rest **200** with the same id |

Held by `uq_documents_user_id_content_hash`, a partial unique index on
`(user_id, content_hash) WHERE deleted_at IS NULL` — not by the lookup, which
concurrent requests can all miss.

A document whose ingestion fails cannot be retried by uploading again; delete it
first.

## When something fails

Filesystem and PostgreSQL cannot share a transaction. The service does not
pretend otherwise; it compensates.

| Failure | Response | Left behind |
|---|---|---|
| Storage fails | **503** | nothing — no row was started |
| Insert/commit fails, this request created the file | **500** | nothing — file deleted |
| Insert/commit fails, file already existed | **500** | the file, which another row of this owner's references |
| Insert loses a race to an identical upload | **200**, the winner | the winner's row and file |
| Cleanup's own delete fails | **500** (the original error) | an orphaned file, logged as `document.upload.orphaned_blob` |
| Redis refuses the job, after the commit *(M3/S3.2)* | **503** | nothing live — the row is soft-deleted and the file removed if this request created it. See [ingestion.md](ingestion.md#when-the-job-cannot-be-queued) |

Not covered, and accepted by ADR-0008 as wasted disk rather than leaked data: a
process crash between storing and committing, and a vanishingly narrow race in
which a request removes a file it created just as an identical concurrent
upload commits a row pointing at it.

## The ingestion boundary

After the commit, the service enqueues an `IngestionJob`:

| Field | Source |
|---|---|
| `document_id` | the committed row |
| `owner_id` | `principal.user_id` |
| `storage_key` | `document_storage_key(...)` |
| `content_hash` | SHA-256 of the validated bytes — lets the worker detect corruption |
| `mime_type` | sniffed, never the client's |

Frozen, and closed to extra fields. It carries no `Principal`, credential or
filename. The worker must use `owner_id` as given and never look an owner up.

**Since M3/S3.2 the queue is `ArqIngestionQueue`** (job id
`ingest-document:{document_id}`), and a worker consumes it: it verifies the
job against the database and the bytes against the hash, and records `failed`
with a reason when they are wrong. **Since M3/S3.3** it then extracts the text,
and **since M3/S3.4** splits it into section-aware chunks: the document becomes
`chunked` — still not searchable, because nothing has embedded it. A document whose job
never reached Redis is picked up by the worker's recovery sweep. The worker is
[ingestion.md](ingestion.md); extraction is [pdf-extraction.md](pdf-extraction.md).
S3.1's `DeferredIngestionQueue`, which logged and did nothing, is gone.

## Logging

`document.upload.stored`, `.deduplicated`, `.rejected` (with a reason code),
`.storage_failed`, `.orphaned_blob`, `.enqueue_failed`, and
`ingestion.job.enqueued` / `.already_queued` — carrying
document id, owner id, content hash, sizes and status. Never the filename,
never file content, never a token; a test asserts all three.

## Discrepancies with the ADRs, recorded

- ~~**ADR-0009 names the terminal state `FAILED`; `DocumentStatus` has `ERROR`.**~~
  **Reconciled in M3/S3.2:** `FAILED`, by migration `0004`.
- ~~**ADR-0009 says upload enqueues to ARQ.**~~ **Done in M3/S3.2** — ARQ, the
  worker process and its compose service.
- **ADR-0008 lists `page_count` without saying when it is set.** It is set at
  upload, because principles.md §4's page cap has to be enforced before storage.
- **Duplicate-row semantics were not specified anywhere.** ADR-0010 proposes
  them and awaits owner review.
- **A transport-level body limit does not exist.** FastAPI spools the multipart
  body to a temporary file before the handler runs, so an oversized upload is
  received in full before it is refused — this process just never loads more
  than the limit into memory. A proxy or middleware limit is M9 hardening.
