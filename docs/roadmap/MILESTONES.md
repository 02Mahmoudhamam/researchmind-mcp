# Milestone Roadmap

Eleven outcome-based milestones from the approved plan. Detailed sprint
breakdowns, acceptance criteria and definitions of done are in
[COMPLETION_PLAN.md](COMPLETION_PLAN.md).

**M0 is a corrective refactor and precedes all feature work.** Code written
against the pre-M0 `mcp/` namespace, `Settings`, or build must be redone.

| # | Milestone | Outcome | Tag |
|---|---|---|---|
| **M0** | Build Integrity & Corrective Refactor | Images build, imports resolve, `pytest` collects, CI green | `v0.1.0` |
| **M1** | System of Record | `User`, `Document`, `DocumentChunk` persist; `BaseRepository` implemented | — |
| **M2** | Identity & Access (Fail Closed) | Forged tokens rejected, proven by test | `v0.2.0` |
| **M3** | Document Ingestion | A real PDF becomes durable, owned, section-aware chunks with honest status | `v0.3.0` |
| **M4** | Vector Index & Isolated Retrieval | Semantic search that provably cannot cross user boundaries | `v0.4.0` |
| **M5** | Grounded Answering | **First working end-to-end flow** — cited answers from real documents | `v0.5.0` |
| **M6** | MCP Adapter | Three working tools reachable from Claude Desktop | `v0.6.0` |
| **M7** | RAG Evaluation | Retrieval and grounding measurable against a committed baseline | `v0.7.0` |
| **M8** | Web Client | The journey is usable in a browser | `v0.7.0` |
| **M9** | Hardening & Observability | Failures visible, bounded and diagnosable | `v0.9.0-rc.1` |
| **M10** | Release Validation | Release gate passed on a clean machine | `v1.0.0` |

## Sprint breakdown

Only recorded where a milestone has been decomposed in practice. The table above
is the original outcome-based definition and is **not** rewritten: it defined
M4 without a sprint breakdown, and that is why ADR-0014 §9 had no sprint to
point at. Sprints are named as they were built.

| Milestone | Sprint | Outcome | State |
|---|---|---|---|
| M3 | S3.1 | Upload, validation, content-addressed storage | Complete |
| M3 | S3.2 | ARQ ingestion worker, claims, reaper, failure reasons | Complete |
| M3 | S3.3 | PDF text extraction into `document_pages`, `parsed` | Complete |
| M3 | S3.4 | Section-aware chunking, provenance, `chunked` | Complete |
| M3 | S3.5 | Embeddings, vector store, `ready` | Complete |
| **M4** | **S4.1** | **Retrieval Foundation** — `SearchService`, PostgreSQL re-validation | **Complete** |
| **M4** | **S4.2** | **Search API & Retrieval Composition Root** — `POST /api/v1/search`, API lifespan owning the provider and Qdrant client | **In progress** |

**M4 is complete when** the HTTP vertical slice works end to end *and* tenant
isolation is proven over HTTP — an authenticated user retrieving only their own
chunks, and a second user issuing the identical search retrieving none of them
(`COMPLETION_PLAN.md` Phase 3 DoD, steps 4–5). Vectors existing in Qdrant is not
the bar; `ADR-0007` §2 requires flat chunk retrieval to *ship*.

## Critical path to the first working flow

```
M0 → M1 → M2 → M3 → M4 → M5
```

Sixteen sprints. M6–M9 are off the critical path to *first working flow*, though
M6 and M10 are on the path to *MVP complete*.

**First working flow** means: register → login → upload a real PDF → parsed,
chunked, embedded, indexed → ask a question → grounded answer with resolving
citations → and a second user provably cannot see any of it.

## Ordering that cannot be relaxed

| Order | Why |
|---|---|
| M1 → M2 | Tokens must bind to persisted users. Verifying a token against nothing is the baseline bug. |
| M2 → M4 | The Qdrant `user_id` filter is only as trustworthy as the identity feeding it. |
| M3 → M4 | Cannot embed chunks that do not exist. |
| M4 → M5 | Cannot build context from retrieval that does not work. |
| M5 → M6 | MCP tools call the services M5 creates. |
| M4 → M7 → *any tuning* | Re-chunking forces a full re-embed. Tuning before measuring buys an expensive guess. |

## Parallelisable

M6, M8 and M9 can all run in parallel after M5 — the largest parallel window.
Golden-dataset curation (M7) can start at the end of M4. Auth UI (M8) can start
at the end of M2.

## Deferred from the MVP — deliberately

| Deferred | Why |
|---|---|
| 4 of 7 MCP tools | Three working tools prove the integration; seven half-working ones prove nothing. Unimplemented tools are **deleted**, not stubbed. |
| Knowledge-graph visualisation, citation export | Pure feature surface; no architectural risk retired. |
| Team workspaces / `workspace` router | No `Session` domain entity exists to back it. |
| Session memory (`RedisMemoryStore`) | Redis earns its place via ARQ and rate limiting. |
| Reranking, hybrid/BM25, parent-document retrieval | Evidence-gated on M7. See [ADR-0007](../adr/0007-defer-parent-document-retrieval.md). |
| Refresh tokens | See [../security/principles.md](../security/principles.md). |
