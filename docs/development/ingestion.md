# Ingestion worker

M3/S3.2, extended by M3/S3.3 and M3/S3.4. What happens to an uploaded document
after `POST /api/v1/documents/upload` has answered 202 — and, as importantly,
what does not happen yet. The stages themselves are
[pdf-extraction.md](pdf-extraction.md), [chunking.md](chunking.md) and
[embeddings.md](embeddings.md).

```text
upload request                                  worker process
──────────────                                  ──────────────
validate → store → INSERT … COMMIT
                     │
                     └─ ArqIngestionQueue.enqueue(IngestionJob)
                          job id "ingest-document:{document_id}"
                                     │
                                  Redis ───────► ingest_document(ctx, payload)
                                                   │ IngestionJob.model_validate
                                                   ▼
                                                 IngestionService.ingest(job)
                                                   │ PostgreSQL (owner-scoped)
                                                   │ Storage.get(storage_key)
                                                   │ SHA-256 verified
                                                   │ PdfTextExtractor (S3.3)
                                                   │ pages + `parsed` committed
                                                   │ DocumentChunker  (S3.4)
                                                   │ chunks + `chunked` committed
                                                   │ EmbeddingProvider (S3.5)
                                                   │ VectorStore.upsert
                                                   ▼
                                                 `ready` committed,
                                                 then returned

                                     cron, every minute: recover_pending_documents
                                                         reap_stale_processing
```

ADR-0009 is the decision. The code:

| File | Role |
|---|---|
| [backend/ingestion/queue.py](../../backend/ingestion/queue.py) | `ArqIngestionQueue` — the producer. The only place the API touches ARQ |
| [backend/ingestion/worker.py](../../backend/ingestion/worker.py) | Task functions. Thin: ARQ call in, `IngestionService` call, return value or `Retry` out |
| [backend/ingestion/worker_settings.py](../../backend/ingestion/worker_settings.py) | `WorkerSettings`, the `arq` entrypoint |
| [backend/services/ingestion_service.py](../../backend/services/ingestion_service.py) | Every decision: ownership, integrity, state, retry classification, what a parse result means. Imports no ARQ, FastAPI, storage backend or PDF library |
| [backend/db/repositories/document.py](../../backend/db/repositories/document.py) | The owner-scoped read, the compare-and-set transition, the reaper's update, the recovery sweep's read |
| [backend/db/repositories/document_page.py](../../backend/db/repositories/document_page.py) | Extracted pages, owner-scoped through the document (S3.3) |
| [document_processing/pdf_parser.py](../../document_processing/pdf_parser.py) | `PyMuPDFTextExtractor` — see [pdf-extraction.md](pdf-extraction.md) |
| [document_processing/chunker.py](../../document_processing/chunker.py) | `SectionAwareChunker` — see [chunking.md](chunking.md) |
| [document_processing/embedder.py](../../document_processing/embedder.py) | `FastEmbedProvider` and its tokenizer — see [embeddings.md](embeddings.md) |
| [vector_db/qdrant/repository.py](../../vector_db/qdrant/repository.py) | `QdrantVectorStore` — the only module that speaks Qdrant |
| [backend/db/repositories/document_chunk.py](../../backend/db/repositories/document_chunk.py) | Chunks, owner-scoped through the document (S3.4) |

## Running it

```bash
docker compose up -d postgres redis worker
# or, on the host:
env -u PYTHONPATH poetry run arq backend.ingestion.worker_settings.WorkerSettings
```

The worker reuses the backend image and mounts the same `./uploads` volume, as
ADR-0009 §1 requires. It must see the same `STORAGE_ROOT`, `DATABASE_URL` and
`REDIS_*` as the API.

## Status lifecycle

ADR-0009 §4: `PENDING → PROCESSING → READY | FAILED`, with the failure reason
persisted — and, since S3.3 and S3.4, `parsed` and `chunked` stages between
(ADR-0011, ADR-0012).

| Status | Meaning | Set by |
|---|---|---|
| `pending` | Recorded; no stage has completed, none is running | Upload. The worker, releasing a claim to retry |
| `processing` | A delivery holds the claim | The worker's claim |
| `parsed` | Text extracted and stored; not chunked, embedded or searchable | The worker, with the pages, in one transaction (S3.3) |
| `chunked` | Split into section-aware chunks and stored; not yet searchable | The worker, with the chunks, in one transaction (S3.4) |
| `ready` | Embedded, vectors persisted — searchable | The worker, **after** the upsert returns (S3.5) |
| `failed` | Can never be processed; `failure_reason` says why | The worker, the reaper |

**`ready` means the vectors are in the store.** The M3 definition of done is
"poll until ready, then search", so `ready` is committed *after* the vector
store has accepted the upsert and nowhere else (ADR-0013 §3). The reverse order
would allow `ready` with no vectors — the one state a client cannot detect. A
test (`test_ready_is_set_only_after_the_vectors_are_written`) and mutations hold
this; until S3.5 the same place held the opposite, that nothing set `ready` at
all. See [embeddings.md](embeddings.md).

**`failed` replaces `error`** (migration `0004`). ADR-0009 names the terminal
state `FAILED`; S3.1 had left `DocumentStatus.ERROR` for the first sprint that set
a failure to reconcile. No code path had ever written `error`, but the migration
converts any such row to `failed` with reason `unknown` anyway, and its downgrade
converts back. The frontend's `DocumentStatus` type was changed to match — the
only frontend edit in S3.2.

`failure_reason` is `VARCHAR(64)`, and `ck_documents_failure_reason_iff_failed`
makes it present **if and only if** the status is `failed`. A failure without a
reason, or a reason left on a document that is not failed, is refused by
PostgreSQL rather than by convention.

## One delivery, in order

1. **Validate the payload** into an `IngestionJob` (frozen, extra fields
   forbidden). A malformed payload is rejected, not retried, and touches no
   document: a job that cannot be parsed cannot be trusted to name the right one.
2. **Check the job against itself.** The storage key is derived —
   `document_storage_key(owner_id, content_hash)` — so a key that does not derive
   from the job's own owner and hash was not built by the upload service.
3. **Read the document scoped to `job.owner_id`**. A job naming the wrong owner
   reads nothing.
4. **Check the job against the row** — owner, content hash, storage key, MIME
   type. A disagreement *rejects the job* and leaves the document untouched: it
   is the job that is wrong, not the owner's document.
5. **Check the state.** `processing` → another delivery holds it; `ready` or
   `failed` → terminal. Nothing is done. Otherwise the delivery runs every
   stage the document is ready for: a `pending` one is parsed, chunked and
   embedded; a `parsed` one is chunked and embedded; a `chunked` one is
   embedded.
6. **Claim**: `pending → processing`, a conditional `UPDATE`, committed. The
   claim comes before the bytes are read, so concurrent deliveries never read,
   hash or parse the same document.
7. **Verify, holding the claim**: MIME type is PDF; the recorded page count is
   within `MAX_PDF_PAGES`; `Storage.get` returns the bytes; their SHA-256
   (computed in a thread) equals the recorded hash.
8. **Extract** (S3.3) — only now, and only from those verified bytes.
9. **Store and advance**: `processing → parsed` and the pages, in one
   transaction, committed.
10. **Chunk** (S3.4): claim `parsed → processing`, read the pages back
    owner-scoped, chunk them, and store the chunks with `processing → chunked`
    in one transaction.
11. **Embed** (S3.5): claim `chunked → processing`; re-chunk first if the
    stored chunks were sized by a strategy or tokenizer this pipeline no longer
    produces; embed the chunk text holding no transaction; upsert the vectors;
    and only then commit `processing → ready` with the model recorded on every
    chunk. Only then does the task return — ARQ never records success for state
    that is not durable.

Each stage is its own claim, transaction and retry, and releases its claim back
to the status it started from, so a retry repeats the stage that failed.

## Outcomes

Returned by the task (and so visible in worker logs), one per delivery:

| Outcome | When | Document afterwards |
|---|---|---|
| `parsed` | Verified, extracted, pages stored (S3.3) — returned only when the delivery stops there | `parsed` |
| `chunked` | Chunked and stored (S3.4) — an internal stage result; a delivery carries on to embedding | `chunked` |
| `ready` | Embedded and the vectors persisted (S3.5) | `ready` |
| `failed` | Permanent failure, or transient on the last attempt | `failed` + reason |
| `rejected` | Payload invalid, or job disagrees with the database | unchanged |
| `skipped_in_progress` | Another delivery holds the claim, or won the race for it | unchanged |
| `skipped_terminal` | Already `ready` or `failed` | unchanged |
| `claim_lost` | The claim was taken away mid-run (by the reaper) | whatever replaced it — never overwritten |

## Failure reasons

Deterministic codes, never messages. No path, filename, exception text, stack
trace or infrastructure detail is ever persisted.

| Reason | Persisted on document? | Retried? |
|---|---|---|
| `content_hash_mismatch` | yes — bytes are not the bytes validated at upload | **never** |
| `storage_missing` | yes — the object is gone | **never** |
| `unsupported_mime_type` | yes | **never** |
| `storage_read_failure` | on the last attempt | yes — `StorageError` |
| `processing_failure` | on the last attempt | yes — any other error while reading, extracting or storing; also recorded when a final attempt gives up on an unclassified error |
| `pdf_open_failed`, `pdf_encrypted`, `page_limit_exceeded`, `pdf_parse_failed`, `pdf_timeout`, `page_count_mismatch`, `no_extractable_text` | yes — see [pdf-extraction.md](pdf-extraction.md#failures) | **never** |
| `parser_unavailable` | on the last attempt | yes — the extraction process died |
| `no_extracted_pages`, `no_chunks_produced` | yes — see [chunking.md](chunking.md#failures) | **never** |
| `no_chunks_to_embed`, `embedding_dimension_mismatch` | yes — see [embeddings.md](embeddings.md#failures) | **never** |
| `embedding_failure`, `vector_store_failure` | yes — see [embeddings.md](embeddings.md#failures) | yes |
| `chunking_failure` | on the last attempt | yes — the chunker or the write failed |
| `stale_processing` | yes — by the reaper | n/a |
| `invalid_job`, `document_not_found`, `ownership_mismatch`, `storage_key_mismatch`, `mime_type_mismatch` | **no** — the job is rejected, the document untouched | **never** |
| `unknown` | only on rows migrated from `error` | n/a |

The API reports `status: "failed"` and does not expose `failure_reason`; a test
asserts neither the reason nor any path appears in the response.

## Retries

ADR-0009 §5: backoff on transient errors, terminal `failed` on permanent ones,
never infinite retry.

- `INGEST_MAX_TRIES` (default 5) attempts in total, including the first.
- Delay before the retry after attempt *n*: 2, 10, 30, then 60 seconds.
- A transient failure **releases the claim** before retrying, so the next
  attempt starts from `pending`, not from a claim the reaper might fail.
- On the final attempt the same failure is recorded as `failed` instead.
- An error the service does not classify — most plausibly the database itself —
  is retried while attempts remain. On the final attempt the worker logs
  `ingestion.gave_up` and, in a fresh session, records the document as `failed`
  (`processing_failure`) — claiming a `pending` one first (S3.3). Left `pending`,
  the recovery sweep would give it a fresh job with a fresh budget: retrying
  forever. If even that write fails (`ingestion.gave_up_unrecorded`), the
  database is still down; the sweep recovers the document once it is back.

An integrity failure is asserted not to retry with attempts remaining, both in
the service and through a real ARQ worker (`jobs_retried == 0`). A rejected job —
including a wrong owner or a malformed payload — returns a result instead of
raising, so ARQ has nothing to retry; the malformed case is also run through a
real worker.

## Idempotency

A job may be delivered more than once — ARQ is at-least-once, and a re-enqueue is
always possible. Three layers, the last of which is the guarantee:

1. **Job id** `ingest-document:{document_id}`. ARQ will not queue a second job
   with an id already queued, deferred for retry, or running — it checks and
   writes in one WATCH/MULTI transaction. `keep_result = 0`, so once a job
   finishes the id is free again rather than blocked for the result's lifetime;
   the recovery sweep depends on that.
2. **State pre-check** (step 5): a redelivery of a claimed or finished document
   is a no-op. Since S3.5 a `chunked` one is not finished — it is embedded.
3. **Compare-and-set claim**: `UPDATE … WHERE status = 'pending'`. Under
   concurrent deliveries exactly one row update succeeds; tests run deliveries
   concurrently and assert at most one ever reads storage, and exactly one
   parses.
4. **The pages' primary key** `(document_id, page_number)`, the chunks'
   `(document_id, chunk_index)`, and since S3.5 the **derived point id**: even a
   defect that let two deliveries through could not store a document's text
   twice, and a second upsert of the same chunk overwrites one point rather
   than creating another (ADR-0013 §4).

Every transition is conditional on the status it expects, so a delivery whose
claim was reaped cannot write `pending` or `failed` over the reaper's decision.

## The recovery sweep (S3.3)

S3.2 left documents stranded `pending` with no job anywhere: an ARQ job
**timeout** cancels the delivery, which releases its claim, and ARQ does not
retry a timed-out job; a **Redis outage** can fall between an upload's commit and
its enqueue; a final attempt could once give up without recording anything.
Nothing looked at those documents again.

- `recover_pending_documents`, a cron job every minute on the half-minute and
  **once at worker startup**, `unique=True`.
- Selects live documents **with stored content** (storage key, hash and type
  all set) in a status a stage can still advance — `pending`, and since S3.4
  `parsed`, and since S3.5 `chunked` — oldest `updated_at` first, at most 500 a
  pass (`RECOVERY_BATCH_SIZE`). Never `processing`: that claim is the reaper's,
  and the repository refuses to be asked for it. Never `ready` or `failed`,
  never deleted, never a row from before upload existed.
- Builds each job **from the row**: its own `user_id`, storage key, hash and
  type. There is no request to take anything from. A row whose storage key is
  not what its owner and hash derive to is **skipped and logged**
  (`ingestion.recovery.skipped`) — a job built from it would be rejected, and
  re-queued, every minute forever.
- Enqueues through the worker's own Redis connection (`ctx["redis"]`,
  `PooledArqIngestionQueue`). **ARQ refuses a duplicate**: a document whose job
  is queued, deferred for a retry or running gets none, however many sweeps
  race — tested with a real deferred retry and with four concurrent sweeps.
- Returns and logs counts: requeued, already queued, skipped.

Like the reaper, its read is cross-tenant maintenance — the third documented
exception to owner scoping below.

## The stale-job reaper

ADR-0009 §6: a document must never be permanently `processing`.

- A cron job every minute, and **once at worker startup**, so a worker returning
  from an outage clears abandoned claims immediately. `unique=True`, so several
  workers do not reap the same minute twice.
- Fails documents `processing` whose `updated_at` is older than
  `INGEST_STALE_PROCESSING_SECONDS` (default 900), with reason
  `stale_processing`. Age is measured against PostgreSQL's clock, not the
  worker's, and every transition sets `updated_at`.
- **It cannot fail a running job.** ARQ cancels a job at
  `INGEST_JOB_TIMEOUT_SECONDS` (default 300), and `Settings` refuses to start
  unless the stale threshold is strictly greater. A claim older than the
  threshold has no worker left.
- It touches only `processing` documents that are not deleted. Never `pending`.

A job cancelled by timeout or shutdown releases its claim first — shielded from
the cancellation — so the reaper is the fallback for a process that died, not
the normal path.

## Ownership

The worker has no request, token or `Principal`. It has `job.owner_id`, written
by the request that authenticated the upload, and the database — which is
authoritative. It never reads an owner from a filename, header, client metadata
or token role, because none of those reach it.

Every worker query that reads or changes a document carries
`documents.user_id = :owner` in its SQL (M1/S1.3); a test captures the emitted
statements and checks. Two deliberate, documented exceptions:

| Method | Why it is not owner-scoped |
|---|---|
| `live_document_exists(document_id)` | Called only after the scoped read found nothing, to tell an operator "wrong owner" from "deleted". Returns a bool, never a row, and does not change the outcome — the job is rejected either way |
| `fail_stale_processing(...)` | Maintenance across every tenant; a stale claim has no owner to act for. Changes only abandoned `processing` rows, and returns ids |
| `list_pending_for_recovery(...)` | The recovery sweep (S3.3). Reads ids, owner, key, hash and type of live `pending` documents so a job can be built from each row's own owner. No content, no filename |

Extracted pages are written and read through `DocumentPageRepository`, which
reaches ownership through the document in SQL; a test checks that the page
insert is preceded by the owner-scoped lookup.

## Storage and parsing

The worker reads bytes only through `Storage`, and parses them only through
`PdfTextExtractor`. `startup` in `worker.py` is the composition root — the one
place `LocalStorage` and `PyMuPDFTextExtractor` are named — and the service
receives the protocols. Architecture tests hold that the service imports no
storage backend or PDF library, that the worker opens no file itself, and that
only the worker constructs the parser.

## When the job cannot be queued

The upload commits the row *before* enqueueing — the worker must be able to see
what it is told about. If Redis then refuses the job, a committed `pending`
document would exist with nothing to process it, ever. So the upload
compensates:

| Step | Effect |
|---|---|
| soft-delete the document, committed | it is not live; the duplicate index no longer holds it |
| delete the file, **if this request created it** | a file another row references is kept |
| answer **503** | "Document processing is unavailable. The upload was not kept." |

Retrying the same upload once Redis is back succeeds with 202. If the
soft-delete itself fails, `document.upload.unqueued_document_left` is logged:
that document stays `pending` until the recovery sweep re-enqueues it.

The API connects to Redis with one attempt and a one-second timeout, so an
outage is a prompt 503 rather than a request held open by connection retries.
The worker, by contrast, retries its connection: waiting for Redis is its job.

## Logging

`ingestion.job.enqueued`, `.job.already_queued`, `ingestion.claimed`, `.parsed`
(with page counts), `.failed`, `.retry`, `.rejected` (at error — a sound system
produces none), `.skipped`, `.claim_lost`, `.interrupted`, `.release_failed`,
`.reaped`, `.gave_up`, `.gave_up_unrecorded`, `ingestion.recovery.requeued`,
`.skipped`, `.pass`, `ingestion.worker.started` / `.stopped`, and
`document.upload.enqueue_failed`. They carry document id, owner id, content hash,
counts and reason codes — never a filename, path, file content or extracted
text, which tests assert. No token or credential reaches the worker to be
logged.

The worker applies `LOG_FORMAT` and `LOG_LEVEL` itself, in `startup`: `main.py`
does it for the API, and the `arq` command does not. arq's own lines — one per
job start and finish — keep arq's format and print a truncated repr of the
payload: ids, content hash and storage key, which is relative to
`STORAGE_ROOT`. Never a filename or file content, because the job carries
neither.

## Not done

- **Retrieval.** M4's: the vectors exist and are owner-filtered, but nothing
  searches them through an API, and ADR-0003 §5's second layer — re-validating
  returned chunk ids against PostgreSQL — is not built.
- **Deleting a document leaves its vectors.** ADR-0003 §6 is still
  half-implemented; `VectorStore.delete_document` exists and the API's delete
  path does not call it.
- ~~A job cancelled by timeout is not retried~~ and ~~verified documents are
  never picked up again~~ — **closed in S3.3** by the recovery sweep. Every
  upload verified by S3.2 is `pending` and is swept up and parsed.
- **Recovery is unbounded for infrastructure faults.** A document whose job keeps
  hanging past the job timeout for reasons outside the document — storage or the
  database hanging — is re-queued each time it is released. Deterministic
  document faults all end in `failed`.
- **A sweep backlog is ordered, not paged.** If more than 500 `pending` documents
  already have jobs, documents without one wait until the queue drains.
- **One Redis connection per API enqueue.** Deliberate for current volume;
  pooling is hardening.
- **`failure_reason` is not in the API response.** Exposing it is a product
  decision about what a user should be told, not a worker concern.
- **No metrics** — logs only.

## Discrepancies with the ADRs, recorded

- **ADR-0009 §3 wraps "every PyMuPDF call" in a thread.** S3.3's PyMuPDF calls
  run in a killable *process* instead, because a thread cannot be stopped when
  a hostile PDF outlives its deadline. Proposed as ADR-0011. SHA-256 still runs
  in `anyio.to_thread.run_sync`.
- **ADR-0009 §4 has no stage between `PROCESSING` and `READY`.** S3.3 adds
  `parsed`, because `READY` means searchable. Proposed as ADR-0011.
- ~~ADR-0009 does not say what a verified-but-unprocessed document is~~ —
  S3.2 released it to `pending`; S3.3 parses it.
