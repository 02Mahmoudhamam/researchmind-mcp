# ADR-0011 — PDF text extraction: a `parsed` stage, per-page storage, a killable parser

- **Status:** Proposed — implemented in Milestone M3/S3.3; supersede if rejected
- **Date:** 2026-09-15
- **Deciders:** Implementation of M3/S3.3, pending owner review
- **Related:** ADR-0003, ADR-0005 (in ADR-0000), ADR-0007, ADR-0008, ADR-0009; Milestone M3/S3.3

## Context

M3/S3.3 is the first sprint that reads a PDF's content. Three decisions had no
answer anywhere in the repository, and one accepted decision did not fit what
measurement showed.

1. **What a successfully parsed, not yet chunked document is.** ADR-0009 §4
   draws `PENDING → PROCESSING → READY | FAILED`. `READY` means searchable: the
   Phase 3 definition of done polls `GET /documents/{id}` "until `status ==
   ready`" and then searches; PROJECT_VISION flips the chip to `READY` after
   parse, chunk, embed and upsert; `DocumentStatus`'s own docstring (S3.2) says
   parsed *and chunked*. Setting `READY` after extraction would tell a polling
   client to search a document with no chunks.
2. **Where extracted text lives.** The schema had `documents` and
   `document_chunks`. A column on `documents` would be loaded by every list
   query. `document_chunks` is chunking's output — with `chunk_index`,
   embedding columns and, per ADR-0007, sections still to come — and writing
   pages there would claim a chunking that has not happened.
3. **What a PDF with no text is.** A scan is a valid PDF. Upload accepts it. OCR
   is out of scope.
4. **ADR-0009 §3 names `anyio.to_thread.run_sync`.** Its concern is the event
   loop, which a thread protects. But extraction is untrusted work, and a
   Python thread cannot be stopped: a hostile PDF that makes MuPDF spin keeps a
   CPU and a thread after any deadline "fires". The job's own timeout would
   cancel the coroutine awaiting it, not the thread — and, worse, a cancelled
   job releases its claim unrecorded, so the recovery sweep would queue the
   same PDF again.

Measured, not assumed (S3.3): on a two-column page PyMuPDF returns blocks in
content-stream order, and `sort=True` interleaves the columns block by block.
ADR-005's "block mode with column-aware ordering" needs an ordering rule of its
own.

## Decision

1. **A `parsed` status**, between `processing` and `ready`: text extracted and
   stored, not yet chunked, embedded or searchable. `ready` keeps its meaning.
   Later stages continue from `parsed`.
2. **`document_pages`**: one row per page, primary key `(document_id,
   page_number)`, `blocks` a JSONB array of `{text, font_size}` in reading
   order, `ON DELETE CASCADE`. The key makes storing a page twice impossible.
   Empty pages are rows with no blocks, so page numbers are never inferred.
   Pages and the move to `parsed` are one transaction.
3. **Blocks carry text and dominant font size only** — page association for
   ADR-0007's `page_start`/`page_end` and for citations, font size for ADR-005's
   "regex + font-size heuristics" heading detection. No coordinates or styles.
4. **Reading order**: a block crossing the middle of the page is read where it
   stands; between such blocks, the left column top to bottom, then the right.
5. **No extractable text is a failure** — `no_extractable_text`, persisted —
   not a `parsed` document with nothing in it.
6. **Extraction runs in a separate process** via `anyio.to_process.run_sync
   (cancellable=True)` under `anyio.fail_after(INGEST_PARSE_TIMEOUT_SECONDS)`.
   Past the budget the process is killed and the document fails as
   `pdf_timeout`. The budget must be below `INGEST_JOB_TIMEOUT_SECONDS`, which
   Settings enforces, so the document records why before ARQ cancels the job.
   Process concurrency equals the worker's job concurrency.

This refines ADR-0009 §3 (the execution boundary) and §4 (the lifecycle); the
rest of ADR-0009 stands.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| `READY` after extraction | Treat parsed as done | Breaks the one contract clients have: "ready, then search". |
| Stay `pending` after extraction; infer the stage from data | "pending and has pages" means parsed | Hidden state: every reader must join to know where a document is, and the recovery sweep would re-queue parsed documents every minute. |
| Extracted text on `documents` | A TEXT or JSONB column | Loaded by every list query; mixes a large derived payload into the record of what exists. |
| Pages written as chunks | Reuse `document_chunks` | Claims chunking happened; collides with ADR-0007's chunk sections and ADR-0005's embedding columns. |
| A row per block | `document_blocks` | Tens of thousands of rows per long document, always read back page by page anyway. |
| Empty extraction is `parsed` | Record zero text as success | A document that can never be found reported as processed — the "reported success, did nothing" failure this project exists to stop. |
| `anyio.to_thread.run_sync` | ADR-0009 §3 as written | Protects the event loop but cannot stop a hostile parse; threads and CPUs leak past every deadline. |
| `sort=True` or stream order | PyMuPDF's own orderings | Both measured wrong on two-column pages. |
| GROBID / `unstructured` / Marker | Layout-aware parsing | ADR-005 option D: heavy, a service or a model; deferred to evidence from M7. |

## Consequences

**Good.** `ready` still means searchable. Extracted text is durable, owned
through its document, stored once, and never half-stored. A hostile PDF costs
at most its budget and one killed process. Scans fail honestly instead of
disappearing into an empty index. Chunking can start from stored pages without
parsing again.

**Bad.** One more status value in the API contract and the frontend type. A
process per concurrent job: PDF bytes cross a pipe, and each process holds its
own MuPDF state. The ordering rule mis-orders three-column layouts and reads
tables column by column. A scanned paper cannot be ingested at all until OCR
exists. A timeout under heavy CPU contention fails a legitimate document
permanently; it must be deleted and uploaded again.

**Neutral.** No memory limit is placed on the extraction process yet. Per-page
JSONB is not queryable text; nothing needs it to be.

## References

- ADR-0009 §3, §4 — execution boundary and lifecycle, refined here
- ADR-005 (ADR-0000) §1, §2 — block mode, column-aware ordering, font-size heuristics
- ADR-0007 §1 — chunk `page_start` / `page_end`
- COMPLETION_PLAN Phase 3 — definition of done, task 3.5
- `docs/development/pdf-extraction.md` — the implementation
