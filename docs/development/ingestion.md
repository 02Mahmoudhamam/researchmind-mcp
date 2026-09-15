# Ingestion worker

M3/S3.2. What happens to an uploaded document after `POST /api/v1/documents/upload`
has answered 202 — and, as importantly, what does not happen yet.

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
                                                   ▼
                                                 status committed, then returned
```

ADR-0009 is the decision. The code:

| File | Role |
|---|---|
| [backend/ingestion/queue.py](../../backend/ingestion/queue.py) | `ArqIngestionQueue` — the producer. The only place the API touches ARQ |
| [backend/ingestion/worker.py](../../backend/ingestion/worker.py) | Task functions. Thin: ARQ call in, `IngestionService` call, return value or `Retry` out |
| [backend/ingestion/worker_settings.py](../../backend/ingestion/worker_settings.py) | `WorkerSettings`, the `arq` entrypoint |
| [backend/services/ingestion_service.py](../../backend/services/ingestion_service.py) | Every decision: ownership, integrity, state, retry classification. Imports no ARQ, FastAPI or storage backend |
| [backend/db/repositories/document.py](../../backend/db/repositories/document.py) | The owner-scoped read, the compare-and-set transition, the reaper's update |

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
persisted.

| Status | Meaning | Set by, in S3.2 |
|---|---|---|
| `pending` | Recorded; not yet processed | Upload. The worker, releasing a claim |
| `processing` | A delivery holds the claim | The worker's claim |
| `ready` | Content processed and searchable | **Nothing yet** |
| `failed` | Can never be processed; `failure_reason` says why | The worker, the reaper |

**S3.2 never sets `ready`.** `ready` means the content has been processed — the
M3 definition of done is "poll until ready, then search", and nothing parses,
chunks or embeds yet. A document whose bytes are verified sound is released back
to `pending`, the only true state for a document nothing has processed. A test
(`test_it_never_sets_ready`) and a mutation hold this.

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
   `failed` → terminal. Nothing is done.
6. **Claim**: `pending → processing`, a conditional `UPDATE`, committed.
7. **Verify, holding the claim**: MIME type is PDF; `Storage.get` returns the
   bytes; their SHA-256 (computed in a thread) equals the recorded hash.
8. **Release**: `processing → pending`, committed. Only then does the task
   return — ARQ never records success for state that is not durable.

## Outcomes

Returned by the task (and so visible in worker logs), one per delivery:

| Outcome | When | Document afterwards |
|---|---|---|
| `verified` | Bytes present and sound | `pending` |
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
| `processing_failure` | on the last attempt | yes — any other error while reading |
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
  is retried while attempts remain. On the final attempt there may be nothing to
  record a failure with, so the worker logs `ingestion.gave_up` and the job
  fails in ARQ; a claim left behind is the reaper's.

An integrity failure is asserted not to retry with attempts remaining, both in
the service and through a real ARQ worker (`jobs_retried == 0`). A rejected job —
including a wrong owner or a malformed payload — returns a result instead of
raising, so ARQ has nothing to retry; the malformed case is also run through a
real worker.

## Idempotency

A job may be delivered more than once — ARQ is at-least-once, and a re-enqueue is
always possible. Three layers, the last of which is the guarantee:

1. **Job id** `ingest-document:{document_id}`. ARQ will not queue a second job
   with an id already queued or running. `keep_result = 0`, so once a job
   finishes the id is free again rather than blocked for the result's lifetime.
2. **State pre-check** (step 5): a redelivery of a finished document is a no-op.
3. **Compare-and-set claim**: `UPDATE … WHERE status = 'pending'`. Under
   concurrent deliveries exactly one row update succeeds; a test runs deliveries
   concurrently and asserts at most one ever reads storage at a time.

Every transition is conditional on the status it expects, so a delivery whose
claim was reaped cannot write `pending` or `failed` over the reaper's decision.

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

## Storage

The worker reads bytes only through `Storage`. `startup` in `worker.py` is the
composition root — the one place `LocalStorage` is named — and the service
receives the protocol. Architecture tests hold that the service imports no
storage backend and that the worker opens no file itself.

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
that document stays `pending` until something re-enqueues it.

The API connects to Redis with one attempt and a one-second timeout, so an
outage is a prompt 503 rather than a request held open by connection retries.
The worker, by contrast, retries its connection: waiting for Redis is its job.

## Logging

`ingestion.job.enqueued`, `.job.already_queued`, `ingestion.claimed`, `.verified`,
`.failed`, `.retry`, `.rejected` (at error — a sound system produces none),
`.skipped`, `.claim_lost`, `.interrupted`, `.release_failed`, `.reaped`,
`.gave_up`, `ingestion.worker.started` / `.stopped`, and
`document.upload.enqueue_failed`. They carry document id, owner id, content hash
and reason codes — never a filename, path or file content, which a test
asserts. No token or credential reaches the worker to be logged.

The worker applies `LOG_FORMAT` and `LOG_LEVEL` itself, in `startup`: `main.py`
does it for the API, and the `arq` command does not. arq's own lines — one per
job start and finish — keep arq's format and print a truncated repr of the
payload: ids, content hash and storage key, which is relative to
`STORAGE_ROOT`. Never a filename or file content, because the job carries
neither.

## Not done, and why it matters for S3.3

- **Nothing takes a verified document further.** It is `pending` after the
  worker, exactly as after upload. The first processing stage (S3.3) goes where
  the release is now; it must also decide how documents verified but not
  processed — every upload between S3.2 and S3.3 — get re-enqueued, because
  nothing sweeps `pending` documents.
- **A job cancelled by timeout is not retried by ARQ.** Its claim is released to
  `pending`, and nothing re-enqueues it — the same gap.
- **A database outage longer than the retry budget** leaves a `pending` document
  unprocessed, or a `processing` one for the reaper to fail as
  `stale_processing`. Either is honest; neither recovers on its own.
- **One Redis connection per enqueue.** Deliberate for current volume; pooling is
  hardening.
- **`failure_reason` is not in the API response.** Exposing it is a product
  decision about what a user should be told, not a worker concern.
- **No metrics** — logs only.

## Discrepancies with the ADRs, recorded

- **ADR-0009 §3 wraps "every PyMuPDF call" in a thread.** S3.2 makes no PyMuPDF
  call; the one CPU-bound step it has, SHA-256 of the whole file, runs in
  `anyio.to_thread.run_sync` on the same principle.
- **ADR-0009 lists `READY` as the success state.** Correct for the pipeline; not
  reachable by a sprint that runs no pipeline. See "Status lifecycle".
- **ADR-0009 does not say what a verified-but-unprocessed document is.** S3.2
  releases it to `pending`, rather than leaving it `processing` (which the reaper
  would, correctly, fail) or inventing a status the ADR does not define.
