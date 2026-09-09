> [!WARNING]
> **SUPERSEDED — retained for provenance.**
> This is the original pre-approval decision record. Its five decisions were
> reviewed, and are now recorded as accepted ADRs in this directory
> (`0001`–`0009`). Where they differ, the numbered ADRs are authoritative.

# Architecture Decision Records

**Date:** 2026-09-08 · **Status of every ADR below: `DEFAULT — pending owner approval`**

These five decisions resolve the architectural forks the audit found unresolved.
[COMPLETION_PLAN.md](../roadmap/COMPLETION_PLAN.md) assumes every default below; each ADR ends with an
**If you override** block naming exactly which phases and files change.

Evidence labels: `[FACT — file:line]` · `[INFERENCE]` · `[ASSUMPTION]`.
Scope labels: **EXISTING** (in the repo now) · **PROPOSED** (this ADR) · **REQUIRED** (must change) · **OPTIONAL**.

| ADR | Decision | Default |
|---|---|---|
| [001](#adr-001) | MCP transport & deployment | Shared service layer; MCP as sibling adapter; **stdio v1**, HTTP deferred |
| [002](#adr-002) | Embedding provider | **Local FastEmbed `bge-small-en-v1.5` (384-d)** behind a provider protocol |
| [003](#adr-003) | Agent design | **Collapse 9 agents → 1 tool-using agent**; keep `BaseAgent` |
| [004](#adr-004) | System of record | **Postgres + SQLAlchemy 2.0 async + Alembic**; Qdrant kept as an index, never authority |
| [005](#adr-005) | Chunking strategy | **Section-aware + token-based + parent-document retrieval** |

---
<a name="adr-001"></a>
## ADR-001 — MCP transport and deployment model

### Context

**EXISTING.** `[FACT — mcp/server/server.py:2-4,77-80]` The server imports the `mcp` SDK and runs
over **stdio only**. `[FACT — docker-compose.yml:17-23]` It is deployed as a long-lived container
with `depends_on: backend`, **no ports**, and nothing attached to stdin. `[FACT — .env.example:14-16;
backend/config/settings.py:22-23]` `MCP_SERVER_HOST`/`MCP_SERVER_PORT=8001` are defined and read into
`Settings`, then **never used**. `[FACT — docs/ARCHITECTURE.md:16-18]` The diagram draws
`FastAPI → "MCP Protocol" → MCP Server`. `[FACT — mcp/client/client.py:7-32]` `ResearchMindMCPClient`
exists to serve that edge; all five methods are stubs and the class is **never instantiated**.

`[INFERENCE]` A stdio MCP server is a subprocess spawned by its client over pipes. As composed,
the container starts, finds no client on stdin, and sits inert. The repo simultaneously
expresses three incompatible intents: stdio (code), network service (env vars, ports, diagram),
and backend-as-MCP-client (the orphaned client class).

`[INFERENCE]` There is a deeper problem than transport. The proposed `FastAPI → MCP → Agents`
path would have the backend serialize a request, cross a process boundary, and land in code it
could have imported directly. That is latency and a failure domain bought for nothing.

### Options

| Option | Description | Latency | Ops complexity | Auth surface |
|---|---|---|---|---|
| **A. stdio, client-spawned** | Claude Desktop spawns the server as a subprocess | 0 network hops | Lowest — no container, no port | None (process-local trust) |
| **B. Streamable HTTP on :8001** | Standalone network service; matches env vars + compose | +1 hop (~1-5 ms LAN) | A service, a port, TLS, auth | **Full** — exposes the whole corpus; none exists today |
| **C. In-process only** | Backend imports tool handlers; no MCP server at all | 0 hops | Lowest | None |
| **D. Shared service layer, two adapters** | `backend/services/*` is the single implementation; REST and MCP are thin adapters over it | 0 hops internally | One extra entry point, no extra container | Deferred until an HTTP transport is actually added |

`[INFERENCE]` C is rejected outright: it discards the project's differentiator, since the whole
point of §1 of the vision is reachability from Claude Desktop.

B is rejected **for v1** on a security argument, not a preference: an HTTP MCP server today
would be an unauthenticated endpoint over the entire document corpus, and `[FACT — audit §4.7]`
the auth layer is fully stubbed. Adding a network transport before auth exists inverts the
correct order of work.

### Decision — `DEFAULT`

**PROPOSED.** Adopt **D**, with **stdio as the v1 transport**.

1. **REQUIRED** — `backend/services/*` becomes the single implementation of every capability.
2. **REQUIRED** — the MCP server is a **sibling adapter**, importing those services directly
   (`mcp_server/tools/semantic_search.py` calls `SearchService`, not HTTP).
3. **REQUIRED** — the FastAPI backend **never speaks MCP**. Delete `mcp/client/` entirely.
4. **REQUIRED** — remove the `mcp-server` service from `docker-compose.yml`; ship the server as
   a documented spawnable command with a Claude Desktop config snippet in the README.
5. **REQUIRED** — remove `MCP_SERVER_HOST`/`MCP_SERVER_PORT` from `Settings` and `.env.example`
   until an HTTP transport actually exists. Dead config that contradicts the code is worse than
   absent config.
6. **OPTIONAL / deferred to Phase 6** — add Streamable HTTP as a *second* transport on the same
   `Server` object once ADR-004 auth is real.

```
        PROPOSED                                    REJECTED (today's diagram)
  ┌──────────────┐   ┌──────────────┐          ┌─────────┐   MCP    ┌─────────┐
  │ REST adapter │   │ MCP adapter  │          │ FastAPI │ ───────► │   MCP   │
  │  (FastAPI)   │   │   (stdio)    │          └─────────┘ protocol │ Server  │
  └──────┬───────┘   └──────┬───────┘             (serialize + cross a process
         └────────┬─────────┘                      boundary to reach your own
          ┌───────▼────────┐                       importable code)
          │ backend/services│  ← single implementation
          └───────┬────────┘
       ┌──────────┼──────────┐
   Postgres    Qdrant    Anthropic
```

### Consequences

**Good.** `[INFERENCE]` Removes a container, a port, an IPC boundary and an entire failure domain.
Deletes ~32 lines of orphaned client code. Makes the Claude Desktop demo — the most compelling
thing this project can show — the *primary* path rather than an afterthought. Business logic
gets exactly one home, so REST and MCP cannot drift.

**Bad.** `[INFERENCE]` The MCP server can only be used by a host on the same machine that can
spawn a subprocess; remote hosts and multi-client sharing are out until Phase 6. Both the MCP
process and the API process load the same service layer, so each holds its own DB and Qdrant
pool — acceptable at this scale, and it is why ADR-004 keeps Postgres as the arbiter.

**REQUIRED doc changes.** `docs/ARCHITECTURE.md:16-18` must be redrawn; the "MCP Protocol" arrow
is wrong under this decision.

### If you override to B (HTTP service)

Then: ADR-004 auth becomes a **hard blocker for Phase 3**, not Phase 2; the MCP server needs its
own auth middleware and token model (an MCP host is not a browser, so the JWT flow does not
transfer); `mcp-server` stays in compose and gains TLS; Phase 3 effort moves S→M and Phase 5
grows a second hardening surface. `[INFERENCE]` I would still sequence stdio first and add HTTP
second, because it is strictly additive on the same `Server` object.

---
<a name="adr-002"></a>
## ADR-002 — Embedding provider

### Context

**EXISTING.** `[FACT — document_processing/embedder.py:9-11]` Default model is
`text-embedding-3-small` with `self._client = None  # TODO: init anthropic/openai client`.
`[FACT — vector_db/qdrant/config.py:12]` `vector_size: int = 1536  # text-embedding-3-small dimension`,
hard-coded. `[FACT — pyproject.toml]` `openai` is **not** declared; `anthropic` **is** declared and
`[FACT — audit §1.2]` never imported. `[FACT — .env.example]` No `OPENAI_API_KEY` exists anywhere.

`[INFERENCE]` Anthropic publishes no embeddings endpoint, so the code's stated intent is
unsatisfiable as written. The system currently requires a second vendor that the configuration
does not acknowledge.

`[INFERENCE]` **This decision is expensive to reverse.** Changing the model changes the vector
dimension, which invalidates every stored vector; a corpus must be fully re-embedded and
re-indexed. Deciding before any real indexing happens is worth real deliberation now.

### Options

| Option | Dim | Marginal cost | Data egress | Added deps | Quality |
|---|---|---|---|---|---|
| **A. OpenAI `text-embedding-3-small`** | 1536 | ~$0.02/1M tok | Every chunk + every query leaves the machine | `openai` + a 2nd API key | Strong general baseline |
| **B. OpenAI `text-embedding-3-large`** | 3072 (truncatable to 1536) | ~6.5× A | same | same | Best of the API options; 2× storage |
| **C. Voyage `voyage-3`** | 1024 | paid | same | `voyageai` + 2nd key | Anthropic's documented partner; strong on technical text |
| **D. Local FastEmbed `BAAI/bge-small-en-v1.5`** | **384** | **$0** | **None** | `fastembed` (ONNX; no torch) | Competitive with A on retrieval benchmarks `[INFERENCE — general knowledge, not repo evidence]` |
| **E. Local `bge-base-en-v1.5`** | 768 | $0 | None | `fastembed` | Slightly better than D, 2× storage, ~2× CPU |

**Cost reality check.** `[INFERENCE]` A 20-page paper ≈ 10k tokens ≈ ~30 chunks. Embedding 10,000
papers with option A costs roughly **$2 total**. Embedding cost is *not* a real constraint at this
project's scale. The variables that actually matter are **vendor coupling, data egress, and
setup friction**.

**Latency.** `[INFERENCE]` Options A–C add a network round trip (~50–150 ms) **in front of every
single query**, on the critical path, before Qdrant is even touched. Option D runs ONNX on CPU
in ~5–15 ms for a single query. For ingest, D is slower per chunk than a batched API call but
needs no rate-limit handling, no retries and no second failure domain.

### Decision — `DEFAULT`

**PROPOSED.** Adopt **D — local FastEmbed `BAAI/bge-small-en-v1.5`, 384 dimensions** — as the
default, behind an explicit provider abstraction.

1. **REQUIRED** — define an `EmbeddingProvider` protocol in `shared/interfaces/` exposing
   `embed_documents()`, `embed_query()`, and — critically — **`dimension` and `model_id` as
   properties**.
2. **REQUIRED** — `vector_db/qdrant/config.py:12` must stop hard-coding `1536`. The collection's
   vector size is **derived from the active provider**. `[INFERENCE]` As written, swapping the
   model produces a Qdrant dimension-mismatch rejection at runtime, not at startup — the worst
   time to find out.
3. **REQUIRED** — persist `embedding_model_id` and `dimension` on the document/chunk records
   (ADR-004) so a model change is *detectable* and a re-index can be driven from data rather
   than from memory.
4. **PROPOSED** — ship `OpenAIEmbeddingProvider` as a documented opt-in behind
   `EMBEDDING_PROVIDER=openai`, so the choice is configuration, not a rewrite.
5. **REQUIRED** — add `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` to `Settings` and `.env.example`.

### Consequences

**Good.** `[INFERENCE]` The headline win: **`docker compose up` works with exactly one secret,
`ANTHROPIC_API_KEY`** — which is both the best possible contributor DX and the honest fulfilment
of the "Claude-native" story the repo tells. No document text ever leaves the machine, which
matters directly for the unpublished-manuscript use case in the vision. Query latency drops by
a network round trip. 384-d vectors are **4× smaller than 1536-d**, cutting Qdrant memory
materially.

**Bad.** `[INFERENCE]` The backend image grows by roughly 150–400 MB (onnxruntime + model
weights), and the model downloads on first run unless baked into the image — which is
**REQUIRED** for reproducible builds and offline start. CPU-bound ingest will be slower than a
batched API for bulk backfills. A 384-d model has less headroom than 1536-d on subtle semantic
distinctions; ADR-005's section-aware chunking and Phase 4's evaluation harness exist partly to
detect if that becomes real rather than theoretical.

**Deliberately deferred.** `[INFERENCE]` Reranking (a cross-encoder over the top ~50) recovers most
of the quality difference between embedding tiers for far less than a model upgrade costs. It
belongs in Phase 6, *after* Phase 4 can measure whether it helps.

### If you override to A/B/C (an API provider)

Then: add `openai` (or `voyageai`) to `pyproject.toml`; add `OPENAI_API_KEY` to `Settings` +
`.env.example` **without a default** so it fails closed; set the provider's dimension (1536 /
3072 / 1024); Phase 3 gains a network failure mode (rate limits, retries, backoff) and Phase 5
gains a cost-tracking metric. `[INFERENCE]` The provider protocol in this ADR means the override
costs one new class and one env var — which is the entire reason for the abstraction.

---
<a name="adr-003"></a>
## ADR-003 — Agent design

### Context

**EXISTING.** `[FACT — verified by normalised diff]` All nine `agents/*/service.py` files are
**43 lines and byte-identical apart from a single docstring line**. `[FACT — agents/*/config.py]`
All nine carry the same `model="claude-sonnet-4-20250514"`, `max_tokens=4096`, `temperature=0.3`.
`[FACT — agents/*/prompt.py:3-9]` All nine system prompts follow one identical three-line template.
`[FACT — audit §1.5]` **No file under `agents/` imports anything outside `agents/` and `shared/`** —
the agents have no tools, no retrieval, and no Anthropic client. `[FACT — backend/services/agent_service.py:3,9]`
Only `OrchestratorAgent` is ever instantiated; the router and seven specialists are unreachable.

`[INFERENCE]` These are not nine capabilities. They are one capability with nine role sentences.

### Options

| Option | LLM calls/request | Latency (p50) | Relative cost | Files to maintain |
|---|---|---|---|---|
| **A. Keep orchestrator → router → 7 specialists** | 3 sequential | ~6–15 s `[INFERENCE]` | ~3× | 36 (9 × 4) |
| **B. Single agent, 7 tools, native tool-use loop** | 1 + tool round trips | ~2–5 s `[INFERENCE]` | ~1× | ~4 |
| **C. Hybrid: single agent default, orchestrator only for genuine multi-step** | 1, occasionally 2 | ~2–5 s typical | ~1.2× | ~8 |

`[INFERENCE]` The decisive argument against A: **the Router is a paid Sonnet call, on the critical
path of every single request, whose entire job is to pick a downstream capability — which is
precisely what Claude's native tool selection already does, better, inside the call you were
making anyway.** `[FACT — agents/router/config.py]` It is configured with `max_tokens=4096` to emit
what should be a single token of classification.

`[INFERENCE]` The argument against A is *not* that multi-agent is wrong in general. Multi-agent
earns its cost when specialists have genuinely different **tools, context windows, or models**.
Here they have none of the three, and `[FACT — audit §4.8]` there is no `asyncio.gather` anywhere,
so not even fan-out parallelism is claimed.

### Decision — `DEFAULT`

**PROPOSED.** Adopt **B**: collapse to a single `ResearchAgent` running a Claude tool-use loop
over the seven capabilities.

1. **REQUIRED** — create `agents/research/` (one package) implementing `BaseAgent`.
2. **REQUIRED** — delete the eight unused agent packages. `[INFERENCE]` Byte-identical templates
   for capabilities that do not differ are a maintenance liability and actively mislead readers
   about what the system does.
3. **REQUIRED — keep** `shared/interfaces/agent.py:BaseAgent` and
   `shared/models/agent.py:{AgentInput,AgentOutput,AgentConfig}`. `[FACT — audit §5.1]` These are
   among the strongest assets in the repo, and they are what makes adding a *justified*
   specialist later a small change rather than a rewrite.
4. **PROPOSED** — the nine system prompts are not wasted: their per-capability guidance moves
   into the seven MCP **prompts** (`mcp_server/prompts/`), which is where MCP intends
   capability-specific instruction to live, and into per-tool descriptions.
5. **REQUIRED** — apply **prompt caching** to the agent's static system prompt.
   `[FACT — audit §3.5]` No caching exists today despite ideal static prompts.
6. **REQUIRED** — read `AgentConfig.retry_attempts`, which `[FACT — shared/models/agent.py:33]` is
   declared and never used, and implement backoff on 429/529.
7. **OPTIONAL, evidence-gated** — if Phase 4 evaluation later shows a genuinely distinct need
   (e.g. `detect_research_gaps` benefiting from extended thinking, or a cheap Haiku pre-filter),
   add that specialist **then**, with the measurement that justified it.

### Consequences

**Good.** `[INFERENCE]` Roughly 3× lower per-request latency and cost. 36 files → ~4. The
architecture starts *matching its own description*: seven MCP tools selected by a model is
exactly what MCP was designed for, so the tool layer and the agent layer stop duplicating each
other.

**Bad.** `[INFERENCE]` Loses the "9-agent system" headline, which has narrative appeal for a
portfolio project. I judge that a net gain: *"I removed eight agents that did nothing and cut
latency 3×"* is a stronger engineering story than a directory listing. It also concentrates
behaviour into one prompt, which makes that prompt a critical asset — mitigated by Phase 4
evaluation.

**REQUIRED doc change.** `docs/AGENTS.md:1-35` describes nine agents and a routing flow that will
not exist; it must be rewritten, not patched.

### If you override to A or C

Then: keep the agent packages but **differentiate them for real** — Router moves to Haiku with
`max_tokens≈64` and `temperature=0` (a classifier, not a generator); specialists get distinct
model/temperature/tool grants. `[INFERENCE]` Phase 3 effort grows L→XL because the vertical slice
must wire three agents instead of one, and Phase 4's evaluation must cover routing accuracy as a
separate metric. Keeping nine *identical* agents is the one variant I would argue against in
any configuration.

---
<a name="adr-004"></a>
## ADR-004 — System of record and ownership binding

### Context

**EXISTING.** `[FACT — audit §2.3]` The repo declares **no relational database, ORM, or migration
tool** — no SQLAlchemy, no asyncpg, no Postgres in `docker-compose.yml`, no Alembic.
`[FACT — backend/api/dependencies/database.py:6-13]` The module named `database.py` yields only the
Qdrant and Redis clients. `[FACT — shared/interfaces/repository.py:9-25]` A generic
`BaseRepository[T, ID]` exists with full CRUD and **zero implementations**.
`[FACT — shared/models/{user,document}.py]` `User` and `Document` are rich persistent entities with
ids, ownership and timestamps.

`[FACT — audit Chain B]` This is the single largest risk in the audit. `DocumentService.get_document(document_id, user_id)`
`[FACT — backend/services/document_service.py:17-19]` has nothing to check ownership *against*, so
client-supplied `document_ids` `[FACT — backend/api/schemas/search.py:11]` become the only scoping
signal — a cross-tenant disclosure by design.

### Options

| Option | Components | Ownership correctness | Ops |
|---|---|---|---|
| **A. Postgres + SQLAlchemy 2.0 async + Alembic; Qdrant for vectors** | 2 stores | Enforced in both; dual-write risk on delete | 2 services |
| **B. Postgres + pgvector only; drop Qdrant** | 1 store | **Structural** — a foreign key and a cascade | 1 service |
| **C. SQLite + SQLAlchemy; Qdrant for vectors** | 2 stores | Same as A | 1 service + a file |

`[INFERENCE]` **B is genuinely tempting and I want to name why.** With pgvector, ownership is a
`WHERE user_id = :id` join and deletion is `ON DELETE CASCADE` — the dual-write class of bug
*cannot occur*. At this project's scale (10³–10⁵ chunks) pgvector with an HNSW index is entirely
sufficient, and it would let `docker-compose.yml` drop a service. Because
`[FACT — shared/interfaces/vector_store.py:7-26]` `BaseVectorStore` already exists, B is
implementable as a `PgVectorRepository` behind the interface the repo already designed.

`[INFERENCE]` I recommend **A** anyway, for three reasons that are about this repo specifically:
the codebase has already committed to Qdrant across compose, `vector_db/qdrant/`, `Settings` and
four docs; Qdrant's payload filtering is materially better suited to the *dominant query shape*
identified in the vision (`document_ids`-scoped search); and ripping out a working, already-wired
component is a large change to impose on a plan whose first principle is *make one thing work
end to end*. B remains a first-class override and I would not argue hard against it.

C is rejected: `[INFERENCE]` SQLite's async story is weaker, and the project already runs
containers, so "no service to run" buys little.

### Decision — `DEFAULT`

**PROPOSED.** Adopt **A**, with an explicit correctness invariant that neutralises the dual-write risk.

1. **REQUIRED** — add Postgres 16 to `docker-compose.yml`; add `sqlalchemy[asyncio]`, `asyncpg`,
   `alembic` to `pyproject.toml`; add `DATABASE_URL` to `Settings` and `.env.example`.
2. **REQUIRED** — ORM models for `User`, `Document`, `DocumentChunk`. The chunk table is not
   redundant with Qdrant: it is what makes reconciliation, parent-document retrieval (ADR-005)
   and re-index-on-model-change (ADR-002) possible.
3. **REQUIRED** — implement `BaseRepository` as `UserRepository` and `DocumentRepository`,
   finally giving `shared/interfaces/repository.py` its implementations.
4. **REQUIRED** — Alembic migrations from the start. `[INFERENCE]` Retrofitting migrations after
   a schema exists in production is far more painful than starting with them.
5. **REQUIRED — denormalise `user_id` and `document_id` into every Qdrant payload**, with
   **keyword payload indexes on both**. `[FACT — audit §4.8]` No payload index is created today;
   without one, `document_ids` filtering degrades to a scan as the corpus grows.

#### The invariant (the heart of this ADR)

> **Postgres is the sole authority for what exists and who owns it. Qdrant is an index, never a
> source of truth. Every retrieval result is validated against Postgres before any content
> reaches the LLM.**

`[INFERENCE]` This gives two independent layers of defence and makes the failure mode safe rather
than catastrophic. The Qdrant `user_id` filter is the *fast* path; the Postgres validation is the
*correct* path. If a delete succeeds in Postgres but fails in Qdrant, the orphaned vectors can
still be *retrieved* — but they are dropped at validation and can never reach a user or a prompt.
The system degrades to "slightly slower and returns fewer results", not "leaks another user's
manuscript". Deletion order is therefore **REQUIRED** to be: soft-delete in Postgres (committed
first) → delete vectors in Qdrant → finalise. Never the reverse.

### Consequences

**Good.** `[INFERENCE]` Unblocks auth (ADR-002 of the audit's P0 list), document listing,
ownership, workspaces and any future multi-tenancy simultaneously — Chain B collapses. Gives
`DocumentStatus.PENDING/PROCESSING/READY/ERROR` `[FACT — shared/models/document.py:15-19]` somewhere
to actually live, which is what makes async ingest observable.

**Bad.** `[INFERENCE]` A third stateful service to run and back up. Two stores mean the
consistency discipline above must be *maintained*, not merely assumed — which is exactly why the
invariant is written down here rather than left to code review. Adds ~M effort to Phase 1.

### If you override to B (pgvector, drop Qdrant)

Then: `docker-compose.yml` drops the `qdrant` service; `vector_db/qdrant/` is replaced by
`vector_db/pgvector/` implementing the same `BaseVectorStore`; `QDRANT_*` settings are removed;
the retrieval-validation step above becomes unnecessary (it is a join), so Phase 3 gets *simpler*
and Phase 1 slightly larger. `[INFERENCE]` Net effort is roughly a wash and the correctness story
is strictly better — this is the override I would most readily accept.

---
<a name="adr-005"></a>
## ADR-005 — Chunking strategy

### Context

**EXISTING.** `[FACT — document_processing/chunker.py:10-12]` `chunk_size=512`, `chunk_overlap=64`,
hard-coded as constructor defaults. `[FACT — audit §4.1]` Neither value appears in `Settings`,
`.env.example`, or any config object — the primary retrieval-quality lever is the least
configurable value in the system. `[FACT — document_processing/chunker.py:14-19]` Two strategies are
declared: `chunk()` (TODO: `RecursiveCharacterTextSplitter`) and `chunk_by_section()` (TODO:
heading-aware). Both stubs; nothing selects between them.

`[FACT]` **The units of `512` are unspecified** — `RecursiveCharacterTextSplitter` counts
*characters* by default, but 512 is conventionally a *token* budget. `[INFERENCE]` A 4× difference
in effective chunk size hinges on an ambiguity no code resolves.

`[INFERENCE]` This is the root of **Chain A**, the audit's dominant quality chain: chunking
propagates to retrieval → context → cost → answer quality, and it is the one decision that is
*cheap now and expensive later*, because re-chunking forces a full re-embed of the corpus.

### Options

| Option | Fit for papers | Effort | Notes |
|---|---|---|---|
| **A. Fixed 512-char recursive (current)** | Poor | — | Domain-blind; splits arguments and reference entries mid-item |
| **B. Token-aware recursive, ~400–500 tok, 15% overlap** | Fair | S | Fixes the units ambiguity; still structure-blind |
| **C. Section-aware + token-based + parent-document retrieval** | **Good** | M | Chunk small for precise embedding; return the enclosing section for generation |
| **D. Layout-aware parsing (GROBID / `unstructured` / Marker) → structured sections** | Best | L | Heavy dependency; a service or a large model |

`[INFERENCE]` Four concrete failure modes of A on this specific corpus:
1. **Two-column PDFs.** PyMuPDF's default text order commonly interleaves columns into
   incoherent text. `[FACT — document_processing/pdf_parser.py:14-16]` No layout handling is
   specified. The chunker sits downstream and will faithfully chunk the garbage.
2. **Section semantics lost.** Abstract / Methods / Results / Discussion are the natural
   retrieval units for every task in the vision; a fixed window ignores them.
3. **Reference lists shredded.** A 512-unit window slices bibliography entries mid-item, which
   directly undermines `extract_citations` — a Tier-1 capability in the vision's §4.5.
4. **Tables, figures, equations** have no handling at all.

### Decision — `DEFAULT`

**PROPOSED.** Adopt **C** for v1; **D** is an OPTIONAL Phase 6 upgrade gated on Phase 4 evidence.

1. **REQUIRED** — parse with PyMuPDF in **block mode with column-aware ordering**, not naive
   `get_text()`. **REQUIRED** — run it via `anyio.to_thread.run_sync`: `[INFERENCE]` PyMuPDF is a
   synchronous C extension and calling it inside `async def parse()` `[FACT — document_processing/pdf_parser.py:10]`
   would block the entire event loop (audit Chain C).
2. **REQUIRED** — detect section headings (Abstract, Introduction, Related Work, Methods,
   Results, Discussion, Conclusion, References) by regex + font-size heuristics; store the
   detected `section` in each chunk's metadata and Qdrant payload.
3. **REQUIRED** — chunk **within** section boundaries, never across them, at **~400 tokens with
   ~15% overlap**, counted with the active embedding model's tokenizer (ADR-002) — so the budget
   is real and comparable to the model's context.
4. **REQUIRED** — handle **References separately**: split on entry boundaries, never mid-entry.
5. **PROPOSED — parent-document retrieval.** Embed and search the small chunks for precision;
   return the **enclosing section** to the LLM for coherence. `[INFERENCE]` This is the single
   highest-leverage element of the decision: it directly serves summarize / compare / gap tasks,
   which need whole arguments rather than fragments, without sacrificing embedding precision.
   The `DocumentChunk` Postgres table from ADR-004 is what makes the parent lookup cheap.
6. **REQUIRED** — move `CHUNK_SIZE_TOKENS`, `CHUNK_OVERLAP_TOKENS`, `RETRIEVAL_TOP_K`,
   `RETRIEVAL_SCORE_THRESHOLD` into `Settings` + `.env.example`. `[INFERENCE]` Phase 4 cannot tune
   what Phase 3 hard-codes.
7. **REQUIRED** — record the chunking strategy version on each document, so a strategy change is
   detectable and a targeted re-index is possible.

### Consequences

**Good.** `[INFERENCE]` Breaks Chain A at its root. Retrieved context becomes argumentatively
whole, so the model stops having to infer connective tissue that chunking removed. Citation
extraction becomes tractable because reference blocks survive intact. `chunk_by_section()`
already exists as a stub `[FACT — document_processing/chunker.py:18]` — the repo anticipated this;
this ADR just makes it the primary path rather than the alternate one.

**Bad.** `[INFERENCE]` Heading detection is heuristic and will fail on unusual layouts, so a
**REQUIRED** fallback to strategy B is needed when no sections are detected — silently producing
one giant chunk would be far worse than degrading to fixed windows. Parent-document retrieval
means larger prompts, so token budgeting in the agent (ADR-003) becomes load-bearing rather than
optional. Section-aware chunking also produces variable-size chunks, which makes `top_k` a less
predictable proxy for context size — Phase 4 should measure *tokens retrieved*, not just `k`.

### If you override to B (token-aware fixed windows)

Then: Phase 3 drops the heading detector and the parent lookup (effort L→M); the `DocumentChunk`
table is still needed for ADR-004 reconciliation but not for parent retrieval; and Phase 4's
baseline should be expected to land materially lower on citation-related questions.
`[INFERENCE]` I would accept B only as an explicit *"ship the slice sooner, re-chunk later"* trade
— and it must be a conscious one, because re-chunking means re-embedding everything.

---

## Cross-ADR consequences

`[INFERENCE]` Three interactions worth stating explicitly, because they are where a change to one
default propagates:

1. **ADR-002 → ADR-005 → ADR-004.** The embedding model fixes the tokenizer, which fixes the
   chunk budget, which fixes what lands in the `DocumentChunk` table. Changing the provider after
   indexing forces a re-chunk *and* a re-embed *and* a re-index. This is why both are decided
   before Phase 3 writes a single vector.
2. **ADR-001 → ADR-003.** Because the MCP adapter calls services directly, the tool layer and the
   agent layer are peers over one implementation, not a stack. This is what makes collapsing nine
   agents safe: no capability lives only inside an agent.
3. **ADR-004 → ADR-001.** Two processes (API and MCP server) sharing one service layer means two
   connection pools against one Postgres. Acceptable at this scale, and it is precisely why the
   ADR-004 invariant names Postgres as the single arbiter of existence and ownership.

## Approval

| ADR | Decision | Approve / Override |
|---|---|---|
| 001 | Shared service layer; MCP sibling adapter; stdio v1; delete `mcp/client/` | ☐ |
| 002 | Local FastEmbed `bge-small-en-v1.5` (384-d) behind a provider protocol | ☐ |
| 003 | Collapse 9 agents → 1 tool-using `ResearchAgent`; keep `BaseAgent` | ☐ |
| 004 | Postgres + SQLAlchemy async + Alembic; Qdrant as index; Postgres-authority invariant | ☐ |
| 005 | Section-aware + token-based chunking with parent-document retrieval | ☐ |

**No code will be written until these are approved or overridden.**
