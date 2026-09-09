# ADR-0009 — ARQ over Redis for asynchronous ingestion

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0001, ADR-0008, Milestone M3

## Context

Document ingestion currently runs **inline in the request handler**, and
`PDFParser.parse()` is declared `async def` while PyMuPDF is a **synchronous C
extension**. Calling it without a thread offload blocks the entire event loop
for the duration of the parse — one large PDF stalls every concurrent request.

Ingestion is also long-running and multi-stage: parse, normalize, section,
chunk, persist, embed, upsert. It needs durability, retry and an honest
terminal state.

Separately, `memory_system/` is **entirely orphaned** — `RedisMemoryStore` has
zero references — so Redis is currently declared infrastructure with no job.

## Decision

Run ingestion in an **ARQ worker over the existing Redis**.

1. Add `arq`; add a `worker` compose service **reusing the backend image**.
2. Upload validates, stores, creates a `PENDING` row, enqueues, and returns
   **202 with the document id**. The client polls status.
3. The worker runs the pipeline, wrapping every PyMuPDF call in
   `anyio.to_thread.run_sync`.
4. Status transitions `PENDING → PROCESSING → READY | FAILED`, with the
   **failure reason persisted**.
5. Retry with backoff on transient errors; **terminal `FAILED` on permanent
   ones — never infinite retry**.
6. A **stale-job reaper** moves any document stuck in `PROCESSING` beyond a
   timeout to `FAILED`. A document must never be permanently `PROCESSING`.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Inline with `to_thread` | Keep it in the request, offload the blocking call | Holds the HTTP connection open for the length of a 200-page parse. Unacceptable client experience and a timeout magnet. |
| FastAPI `BackgroundTasks` | Built in, no new dependency | **The tempting answer and the wrong one.** Runs in the API process with no durability: a deploy or crash mid-ingest leaves documents `PROCESSING` **forever**, with no retry and no record — precisely the class of silent failure this project exists to stop producing. |
| Celery | Mature, batteries included | Heavier than needed; not async-native; would add a broker configuration surface for capability ARQ already covers. |
| Dramatiq / RQ | Lightweight queues | RQ is not async-native. Dramatiq is reasonable but ARQ is purpose-built for asyncio and pairs directly with the Redis already declared. |

## Consequences

**Good.** Uploads return immediately regardless of file size. The event loop is
never blocked by parsing. Jobs survive an API restart, retry with backoff, and
always reach a terminal state. **Redis becomes load-bearing infrastructure
rather than orphaned** — this converts a dead dependency instead of adding a
new one. Per-stage timing telemetry comes almost free.

**Bad.** A second process type to run, deploy and monitor. Ingestion becomes
eventually-consistent, so the client must poll and the UI must render
`PROCESSING` and `FAILED` states honestly. Redis becomes a availability
dependency for ingestion.

**Neutral.** Redis's role is now explicitly job queue plus rate limiting — not
a data store. Session memory remains deferred.
