# CLAUDE.md — ResearchMind MCP

Persistent context for future sessions. Derived from a read-only audit (2026-09-08).

## Companion documents — read these before planning or writing code

| Doc | What it holds |
|---|---|
| [docs/architecture/AUDIT-2026-09.md](docs/architecture/AUDIT-2026-09.md) | Full evidence-based audit: what exists, what is stubbed, trade-offs, P0/P1/P2 gap analysis |
| [docs/roadmap/PROJECT_VISION.md](docs/roadmap/PROJECT_VISION.md) | What the system is for, the end-to-end user journey, and where the stated vision contradicts itself or is over-scoped |
| [docs/adr/0000-original-decision-record.md](docs/adr/0000-original-decision-record.md) | **ADR-001…005 — the five architectural forks.** All marked `DEFAULT — pending owner approval` |
| [docs/roadmap/COMPLETION_PLAN.md](docs/roadmap/COMPLETION_PLAN.md) | Phased roadmap 0–6, vertical-slice-first, with a Definition of Done per phase |

> **Status: the five ADRs are NOT yet approved. No implementation should begin until the owner
> approves or overrides them** — every phase in the completion plan assumes those defaults.

---

## ⚠️ Read this first

**This repository is an architectural skeleton, not a working system.**
2,842 lines / 152 files. **92 `TODO` markers, 99 bare-`...` function bodies.**

Everything that is *complete* is a type declaration, config object, prompt string, or wiring
shim. Everything that would *do work* — parse a PDF, chunk, embed, upsert, search, call
Claude, sign a JWT, check a permission, dispatch an MCP tool — is a stub.

All nine `agents/*/service.py` files are **byte-identical apart from one docstring line**,
and every file's mtime falls in a single ~5-minute window. This tree was generated from a
template in one pass.

**The docs describe the system in the present tense as though it works. It does not.**
When reasoning about this repo, trust the code, not `README.md` / `docs/*.md`.
`docs/roadmap/LEGACY-ROADMAP.md` is the only honest signal — Phase 1 items are all unchecked.

Not a git repository (no `.git`). No CI. No `LICENSE`. No `.gitignore`. No lock file.

---

## Intent

An MCP-based AI research assistant: researchers upload papers/PDFs, then a multi-agent
system summarizes, compares, extracts citations, builds knowledge graphs, and finds gaps
across their corpus. MCP is the integration seam — the same seven capabilities are exposed
as MCP tools so both the project's own Next.js UI and external MCP hosts (e.g. Claude
Desktop) can drive them.

- **User:** individual academic / small research group (`RESEARCHER` is the central role).
- **Query patterns:** (a) corpus-wide semantic search; (b) *document-scoped* agent tasks
  over an explicit `document_ids` list — (b) is the dominant one.
- **Scale target:** never stated anywhere. Roadmap defers multi-tenancy, job queues and
  Qdrant clustering to "Phase 3", so current target is single-node / single-tenant.

---

## Stack

Python `^3.12`, Poetry, FastAPI + Uvicorn, `mcp` SDK, `anthropic`, Pydantic v2 +
pydantic-settings, `qdrant-client`, `redis`, `pymupdf`, `langchain-text-splitters`,
`PyJWT`, `bcrypt`, `structlog`, `httpx`.
Frontend: Next.js 14 (App Router), React 18, TypeScript, Tailwind, axios, react-query,
zustand, recharts.

**Declared but never imported:** `anthropic` (M5's), and `langchain-text-splitters`, which
M3/S3.4 did **not** need: chunking counts tokens through its own `Tokenizer` seam, so that
dependency is now unused *and* unnecessary — a removal candidate for a later sprint. `pymupdf` is imported as of M3/S3.1 by upload validation
(page count and password check) and, as of M3/S3.3, by text extraction in the ingestion
worker; `python-multipart` is exercised by the upload route. `passlib`
was on this list from the scaffold onward and is **gone as of M2/S2.3**, replaced
by `bcrypt` used directly: it was never imported once, and left installed it
selects a hashing backend at import time, falling through to stdlib `crypt` —
removed in Python 3.13.
~~**Required but undeclared:** `openai`~~ — **RESOLVED in M3/S3.5.** The OpenAI
default is gone: embeddings are ADR-0004's local FastEmbed
`BAAI/bge-small-en-v1.5` (384 dimensions), and no `OPENAI_API_KEY` exists
anywhere. `email-validator` was also missing and is now declared via
the `pydantic[email]` extra (M0/S0.1); before that, `shared/models/user.py` and
`backend/api/schemas/auth.py` could not be imported at all.

---

## Entry points

| Purpose | Command | File |
|---|---|---|
| Backend API | `python main.py` → Uvicorn on `APP_PORT` (8000) | [main.py](main.py) |
| FastAPI app object | `backend.api.app:app` | [backend/api/app.py](backend/api/app.py) |
| MCP server (stdio) | `python -m mcp_server.server.server` | [mcp_server/server/server.py](mcp_server/server/server.py) |
| Ingestion worker (M3/S3.2) | `arq backend.ingestion.worker_settings.WorkerSettings` | [backend/ingestion/worker_settings.py](backend/ingestion/worker_settings.py) |
| Fetch the embedding model (M3/S3.5) | `python scripts/fetch-embedding-model.py` | [scripts/fetch-embedding-model.py](scripts/fetch-embedding-model.py) |
| Frontend | `npm run dev` in `frontend/` | [frontend/package.json](frontend/package.json) |
| All services | `docker-compose up -d` — **currently broken, see below** | [docker-compose.yml](docker-compose.yml) |

Routes are mounted under `/api/v1/{auth,documents,agents,search,workspace}`.

## How to run (and why it doesn't yet)

`cp .env.example .env && docker-compose up -d` is the documented path. It fails:

1. ~~**`poetry check` fails**~~ — **RESOLVED in M0/S0.1.** The invalid `python` key was
   removed from `[tool.poetry]`; `poetry check` now returns "All set!", `package-mode = false`
   is declared, and a `poetry.lock` is committed.
2. ~~**`import mcp` resolves to this repo's `mcp/` directory**~~ — **RESOLVED in M0/S0.2.**
   The package was renamed to `mcp_server/`; `import mcp` now resolves to the SDK (1.12.4)
   and every `mcp_server` module imports.
3. ~~**Frontend build breaks**~~ — **RESOLVED in M0/S0.4.** `next.config.mjs` sets
   `output: 'standalone'`, so `Dockerfile.frontend` finds what it copies;
   `postcss.config.js` + `tailwind.config.ts` (v3 format, for the pinned 3.4.x) make
   Tailwind actually compile — previously `next build` exited 0 while emitting the literal
   text `@tailwind base;…` and zero utility classes. `frontend/package-lock.json` is
   committed, so the image's `npm ci` no longer fails outright. The frontend image builds
   and serves; the **backend** half of `docker-compose up` is still unproven.

Also note: **without a `.env`, almost nothing imports.** `ANTHROPIC_API_KEY` has no default,
and five modules call `get_settings()` at import time (`main.py:6`, `backend/api/app.py:7`,
`backend/security/jwt_handler.py:8`, `vector_db/qdrant/config.py:5`,
`memory_system/redis/config.py:5`). Test collection previously failed for this reason; `tests/conftest.py` (M0/S0.1) now supplies
deterministic settings so the suite no longer depends on a developer's `.env`.

Tests: `pytest` (`pytest.ini` sets `asyncio_mode = auto`, `testpaths = tests`).
Two of the four tests fail by construction; the two that pass are vacuous (they assert a
stub returns the right type). There is **no RAG evaluation of any kind**.

---

## Key modules

| Area | Path | State |
|---|---|---|
| Domain models | [shared/models/](shared/models/) | **Complete** — best asset in the repo. `principal.py` (M2/S2.2) is the authenticated identity, distinct from `User` on purpose |
| Interfaces (ABCs) | [shared/interfaces/](shared/interfaces/) | **Complete** — `BaseAgent`, `BaseVectorStore`, `BaseMemoryStore`. `BaseRepository` is **deliberately unimplemented**: its ID-only signatures cannot satisfy the ownership invariant (M1/S1.3) |
| Repositories | [backend/db/repositories/](backend/db/repositories/) | **Complete (M1/S1.3)** — user, document, chunk; ownership in the SQL, not in a Python check. M3/S3.1 added the document storage fields and `find_live_by_content_hash_for_user`; M3/S3.2 the worker's owner-scoped read, compare-and-set status transition and stale-claim reaper update; M3/S3.3 `DocumentPageRepository` (extracted pages, owner-scoped through the document) and the recovery sweep's read |
| Storage | [backend/storage/](backend/storage/), [shared/interfaces/storage.py](shared/interfaces/storage.py) | **Complete (M3/S3.1)** — `Storage` protocol and `LocalStorage`, content-addressed `{user_id}/{sha256}.pdf` (ADR-0008); atomic writes; filename never forms a path |
| Upload validation | [document_processing/validation.py](document_processing/validation.py) | **Complete (M3/S3.1)** — magic bytes, size cap, page cap, password-protected refusal, filename sanitising. Structure only; parsing is still a stub |
| Ingestion worker | [shared/models/ingestion.py](shared/models/ingestion.py), [backend/ingestion/](backend/ingestion/), [backend/services/ingestion_service.py](backend/services/ingestion_service.py) | **Complete (M3/S3.2)** — `ArqIngestionQueue`, an ARQ worker and compose service. Verifies each job against the database (owner-scoped) and the stored bytes against their SHA-256, persists `failed` with a reason code, retries transient failures a bounded number of times, reaps stale claims, and re-queues documents no job will pick up (`pending` since M3/S3.3, `parsed` since M3/S3.4, `chunked` since M3/S3.5). A verified PDF is parsed (S3.3), chunked (S3.4) and embedded (S3.5) by the same delivery and becomes **`ready`** — which means searchable, and is committed only after the vectors are persisted. See [docs/development/ingestion.md](docs/development/ingestion.md) |
| Chunking | [document_processing/chunker.py](document_processing/chunker.py), [document_processing/sections.py](document_processing/sections.py), [document_processing/tokenization.py](document_processing/tokenization.py) | **Complete (M3/S3.4)** — deterministic heading detection, token windows inside sections, references split on entry boundaries, fixed-window fallback; behind a `Tokenizer` seam that since M3/S3.5 holds the **embedding model's own** WordPiece tokenizer (`regex-word/v1` remains for callers that must load no model). Chunks carry section, page span, token count, strategy and tokenizer, and their ids are derived from those, so a version change re-chunks and re-embeds deterministically. See [docs/development/chunking.md](docs/development/chunking.md) and ADR-0012 |
| PDF text extraction | [document_processing/pdf_parser.py](document_processing/pdf_parser.py), [shared/interfaces/pdf_extraction.py](shared/interfaces/pdf_extraction.py) | **Complete (M3/S3.3)** — PyMuPDF block mode, column-aware reading order, cleaned text, per-page storage in `document_pages`; runs in a killable process under `INGEST_PARSE_TIMEOUT_SECONDS`. No OCR: a scan fails as `no_extractable_text`. See [docs/development/pdf-extraction.md](docs/development/pdf-extraction.md) and ADR-0011 (proposed) |
| Settings | [backend/config/settings.py](backend/config/settings.py) | **Complete** (insecure secret defaults); validates `DATABASE_URL` uses the asyncpg driver |
| DB infrastructure | [backend/db/](backend/db/) | **Complete (M1/S1.1)** — `Base` + naming convention, lazy async engine, session factory. Models and migrations `0001`–`0006` since (head `0006`, M3/S3.4: chunk provenance columns and the `chunked` status) |
| API routers | [backend/api/routers/](backend/api/routers/) | `auth`, `health`, and documents list/get/delete implemented. All 9 protected routes are authorised by permission; upload, search, agents and workspace answer **501** |
| API schemas | [backend/api/schemas/](backend/api/schemas/) | **Complete** |
| Services | [backend/services/](backend/services/) | `DocumentService` reads/deletes via repositories and owns the transaction (M1/S1.4); `upload_and_process` validates, stores, records and enqueues (M3/S3.1). Every method takes a **`Principal`, never a `user_id`** (M2/S2.5). **`AuthService` complete (M2/S2.4)** — register and login, bcrypt, JWT issuance. **`IngestionService` complete (M3/S3.2–S3.5)** — the worker's decisions, committing each transition, parsing, chunking and embedding through protocols; imports no ARQ, FastAPI, storage backend, PDF library, embedding model or vector database. **`SearchService` complete (M4/S4.1)** — owner-scoped retrieval; imports no `qdrant_client`, `fastembed`, `numpy` or `vector_db` |
| Security | [backend/security/](backend/security/) | `jwt_handler` complete (M2/S2.1); `authentication` + `get_current_user` complete and fail-closed (M2/S2.2); `passwords` complete — bcrypt, 12-char/72-byte policy, NFKC (M2/S2.3); **`rbac` + `require_permission` / `require_role` complete (M2/S2.5)** — 401 vs 403, role read fresh from the DB |
| RAG pipeline | [document_processing/](document_processing/) | Extraction (S3.3), chunking (S3.4) and embedding (S3.5) complete; metadata extractor and `pipeline.py` **still stubs** |
| Embeddings | [document_processing/embedder.py](document_processing/embedder.py), [shared/interfaces/embedding.py](shared/interfaces/embedding.py) | **Complete (M3/S3.5)** — `FastEmbedProvider` over local `bge-small-en-v1.5`, dimension **measured** at startup, and `FastEmbedTokenizer`, which is what sizes chunks. See [docs/development/embeddings.md](docs/development/embeddings.md) and ADR-0013 |
| Retrieval | [backend/services/search_service.py](backend/services/search_service.py), [shared/models/retrieval.py](shared/models/retrieval.py) | **Complete (M4/S4.1)** — embeds the query with the same provider, searches Qdrant owner-filtered, then re-validates every candidate against PostgreSQL (ownership, not deleted, `ready`, same model) in one batched statement. The Qdrant payload is never read for authorization. No HTTP route yet: `/api/v1/search` answers 501 (ADR-0014 §9). See [docs/development/retrieval.md](docs/development/retrieval.md) |
| Vector store | [vector_db/qdrant/](vector_db/qdrant/), [shared/interfaces/vector_store.py](shared/interfaces/vector_store.py) | **Complete (M3/S3.5)** — `VectorStore` protocol with `owner_id` **required** on every operation that can reach a vector, applied inside the Qdrant filter; collection built from the provider's dimension. The scaffold's optional `filters` dict and hard-coded 1536 are gone |
| Memory | [memory_system/redis/](memory_system/redis/) | Client + config complete; store stubbed **and orphaned** |
| Agents (×9) | [agents/](agents/) | Identical templates; prompts + configs complete, `run()` is a stub |
| MCP tools/resources/prompts | [mcp_server/](mcp_server/) | Importable since M0/S0.2; `list_tools` returns all 7 schemas. Handlers still stubs (M6) |
| Logging | [shared/utils/logger.py](shared/utils/logger.py) | Complete, invoked — but `get_logger()` is never called |
| Health/metrics | [devops/monitoring/health.py](devops/monitoring/health.py) | **Router never mounted** |

### Orphaned code — defined, zero references anywhere
`RedisMemoryStore`, the health router,
`devops/logging/logging_config.py` (duplicate of `shared/utils/logger.py`), and **8 of the
9 agents** (only `OrchestratorAgent` is ever instantiated).

**No file under `agents/` imports anything outside `agents/` and `shared/`.** The agent
layer is fully decoupled from MCP tools, Qdrant, Redis and the Anthropic SDK.

---

## Architecture as actually wired

```
Next.js ──HTTP──► FastAPI :8000 ──► routers ──► services ──┬─► DocumentProcessingPipeline ──► Qdrant
                                                            ├─► SearchService ──► Qdrant
                                                            └─► AgentService ──► OrchestratorAgent
```

Edges that **do not exist** despite being documented:
- FastAPI ⇸ MCP server (no client is ever constructed)
- Orchestrator ⇸ Router ⇸ 7 specialists (never instantiated)
- Agents ⇸ Qdrant / Redis / Anthropic
- **SearchService ⇸ generation** — `AgentInput.context` is never populated by any code path,
  so the retrieval and generation halves of the RAG system are not connected even in stubs.

### Missing components (findings, not blanks to fill)
- **No service wiring** — *narrowed by M1/S1.1, S1.2 and S1.3.* PostgreSQL 16 is a compose
  service with a healthcheck, `DATABASE_URL` is a `Settings` field, `backend/db/` provides
  the declarative `Base`, a lazily-built async engine and a session factory, and
  `backend/api/dependencies/database.py` yields `get_db_session`. The schema exists —
  `users`, `documents` and `document_chunks` from Alembic revision `0001`, ownership as
  foreign keys (`documents.user_id` RESTRICT, `document_chunks.document_id` CASCADE) — and
  `backend/db/repositories/` reads and writes it, with `user_id` a required parameter on
  every method that can reach an owned row **and present in the SQL predicate**.
  `DocumentService` is wired to those repositories (M1/S1.4) and owns the transaction: it
  commits, repositories only flush. Authentication above it is real as of M2/S2.2: every
  protected route now resolves a `Principal` or returns 401, and is authorised by
  permission or returns 403 (M2/S2.5). `DocumentService` receives that `Principal` and
  unwraps `principal.user_id` for the unchanged repositories, and the document list, get
  and delete routes use it — and since M3/S3.1 upload does too, validating, storing at
  `{user_id}/{sha256}.pdf` and recording a `pending` document. Since M3/S3.2 an ARQ
  worker checks each upload's ownership and bytes, since M3/S3.3 extracts its text,
  since M3/S3.4 chunks it and since M3/S3.5 embeds it into Qdrant and marks it
  `ready`; nothing searches it yet. And
  tokens are obtained over HTTP as of M2/S2.4: `POST /api/v1/auth/register`
  creates an account and `POST /api/v1/auth/login` issues a 60-minute access
  token that `get_current_user` accepts. `SearchService` remains a stub (M4).
- **No route to search, and no generation.** Both halves of retrieval now exist:
  the write half through the ingestion worker (M3/S3.2–S3.5), and since M4/S4.1 the
  read half — `SearchService` embeds a query, searches Qdrant filtered on `user_id`,
  and re-validates every candidate against PostgreSQL before returning it, which is
  ADR-0003 §5's invariant in full. What is missing above it: no HTTP route or MCP tool
  calls it (`/api/v1/search` answers 501, ADR-0014 §9), and `AgentInput.context` is
  still populated by nothing, so retrieval and generation remain unconnected (M5).
  Deleting a document still leaves its vectors behind (ADR-0003 §6) — S4.1 makes that
  harmless rather than fixed.
- **No reranker, no hybrid/BM25 search, no query expansion.**

---

## RAG parameters (where they actually live)

| Stage | Value | Location | Note |
|---|---|---|---|
| Chunking | `CHUNK_SIZE_TOKENS=400`, `CHUNK_OVERLAP_TOKENS=60` | `backend/config/settings.py` | M3/S3.4. Counted by the **embedding model's** tokenizer since M3/S3.5; the worker refuses to start unless a chunk plus its special tokens fits the model |
| Embeddings | `BAAI/bge-small-en-v1.5` (384-d) | `backend/config/settings.py`, `document_processing/embedder.py` | M3/S3.5. Local FastEmbed (ADR-0004); weights baked into the image; dimension measured at startup, never configured |
| Vector store | Qdrant, Cosine, dimension from the provider | `vector_db/qdrant/` | M3/S3.5. The hard-coded 1536 was removed; an architecture test scans for the literal |
| Retrieval | `RETRIEVAL_TOP_K=10`, `RETRIEVAL_SCORE_THRESHOLD=0.7`, `RETRIEVAL_MAX_QUERY_CHARS=2000` | `backend/config/settings.py` | M4/S4.1. Validated at startup and overridable per query; the threshold is a Qdrant cosine similarity, not a probability. Dense-only; no HTTP route yet |
| Generation | `claude-sonnet-4-20250514`, `max_tokens=4096`, `temp=0.3` | `agents/*/config.py` | Identical across all 9 agents, including the Router |

`ensure_collection_exists()` was a stub that nothing called. As of M3/S3.5 the
worker's startup calls `VectorStore.ensure_collection(dimension=…)`, so a fresh
Qdrant is prepared before any document is claimed, and a collection of the wrong
width is refused rather than reused.

---

## Conventions

- **Layout:** one directory per bounded context; agents follow a strict 4-file pattern —
  `interface.py` (ABC subclass with `name`/`description`), `config.py` (an `AgentConfig`),
  `prompt.py` (`SYSTEM_PROMPT` + `build_prompt`), `service.py` (the concrete class).
- **Async everywhere** for I/O (`AsyncQdrantClient`, `redis.asyncio`, `async def` routes).
  Caution: `pymupdf` is a sync C-extension — calling it inside `async def` without an
  offload blocks the event loop. Upload validation offloads to a thread; text extraction
  (M3/S3.3) runs in a killable process, because a thread cannot be stopped past a deadline.
- **Stub idiom:** unimplemented bodies are a bare `...` with a `# TODO:` comment naming the
  intended approach. These TODOs are the design record — read them before implementing.
- **Interface-first:** define the ABC in `shared/interfaces/`, implement in the owning package.
- **Typed config:** one `Settings` class, `@lru_cache()`d. Note `QdrantConfig`/`RedisConfig`
  bind `settings.X` as class-attribute defaults **at import time**, which freezes them and
  defeats per-test overrides. `backend/db/engine.py` deliberately does not follow that
  pattern — it reads settings inside an `@lru_cache`d factory, so importing it neither
  freezes configuration nor opens a connection pool (asserted by test in a subprocess).
- **Tooling — enforced since M0/S0.5:** `ruff` (line-length 88) and `black` (same 88) both
  pass and run on every push and PR via `.github/workflows/ci-backend.yml`, alongside
  `poetry check`, `poetry check --lock`, a runtime `import mcp` assertion and `pytest`.
  Frontend lint/type-check/build are enforced by `ci-frontend.yml`.
  **`mypy --strict` is enforced over the finished surface only**: M2's security files
  (M2/S2.5), M3/S3.1's upload, storage and ingestion boundary, M3/S3.2's ingestion
  service, queue and worker, M3/S3.3's extraction, extraction models and page
  persistence, M3/S3.4's chunker, sections, tokenizer and chunk persistence, and
  M3/S3.5's embedding provider, vector-store seam and Qdrant adapter, and
  M4/S4.1's search service, retrieval models and chunk validation pass
  strict with no ignores, and CI fails if they stop.
  Repository-wide `mypy .` reports **104 errors** (was 113 before M3/S3.5, which
  replaced several stubs with real code), mostly `empty-body` in M4–M6
  stubs, and is not enforced; each sprint adds the files it makes real to the list in
  `ci-backend.yml`.
  CI also runs its own **Qdrant** service and caches the embedding model's
  weights, with `REQUIRE_QDRANT=1` and `REQUIRE_EMBEDDINGS=1` so the tests that
  need them fail rather than skip.

---

## Top blockers (P0 — full list in the audit)

1. ~~**`mcp/` shadows the `mcp` SDK**~~ — **RESOLVED in M0/S0.2.** Renamed to `mcp_server/`.
2. ~~**`pyproject.toml:6`**~~ — **RESOLVED in M0/S0.1.**
3. ~~**No system of record**~~ — **RESOLVED across M1/S1.1–S1.3.** Connection, schema and
   ownership-scoped data access all exist; ownership is a database constraint *and* a query
   predicate. What remains is wiring services to it (S1.4) and binding tokens to persisted
   users (M2).
4. ~~**Auth fails open**~~ — **RESOLVED in M2/S2.2.** `get_current_user` returns a
   `Principal` or raises; every protected route answers a forged, expired, tampered or
   unresolvable token with **401**. Identity is resolved by
   `backend/security/authentication.py`, which imports no FastAPI so the MCP adapter can
   reuse it (ADR-0002 §6). Role and email come from the database row, not the token.
5. ~~**Embedding provider unresolved**~~ — **RESOLVED in M3/S3.5.** ADR-0004's
   local FastEmbed `bge-small-en-v1.5`, behind an `EmbeddingProvider` protocol,
   with the dimension derived from the provider (ADR-0005) rather than a literal.
6. ~~**Transport contradiction**~~ — **RESOLVED in M0/S0.2.** ADR-0002 settled on stdio; the
   `mcp-server` container, `Dockerfile.mcp` and `MCP_SERVER_HOST`/`PORT` are removed.
7. ~~**No `LICENSE`**, no `.gitignore`, no `.dockerignore`~~ — **RESOLVED in the Repository
   Foundation phase (v0.0.1).** All three are committed.
8. ~~**Frontend cannot build or style** (P0-12 in the audit)~~ — **RESOLVED in M0/S0.4.**
   See "How to run" above.

## Plan of record

Phased roadmap lives in [docs/roadmap/COMPLETION_PLAN.md](docs/roadmap/COMPLETION_PLAN.md). Summary:

| Phase | Goal | Effort |
|---|---|---|
| 0 | Foundations & unblock — `git init`, LICENSE, `.gitignore`, lock file, fix `pyproject.toml:6`, rename `mcp/` → `mcp_server/`, make the build pass | S/M |
| 1 | System of record — Postgres + SQLAlchemy async + Alembic; `BaseRepository` implementations; ownership checkable | M |
| 2 | Auth done correctly — fail-closed JWT verification bound to the DB; RBAC enforced | M |
| 3 | **📸 Vertical RAG slice** — one PDF → one tool → a cited answer, via REST *and* Claude Desktop | L |
| 4 | RAG evaluation harness — golden set + retrieval/answer metrics, so tuning becomes measurable | M |
| 5 | 🏭 Production hardening — failure modes, observability, secrets, and CI running the already-configured ruff/mypy --strict/pytest | L |
| 6 | Breadth & OSS polish — remaining six tools, honest README, contributor DX | L |

**Phases 0–3 are the minimum credible portfolio milestone.** Phases 0–5 earn the
"production-grade" claim the README already makes.

Non-negotiable ordering: 1 before 2 (tokens must bind to persisted users); 2 before 3 (the Qdrant
`user_id` filter needs a trustworthy identity); 4 before any retrieval tuning (re-chunking forces a
full re-embed, so guessing is expensive).

## Working agreements

- Cite `file:line` for claims about this codebase; distinguish fact from inference.
- Do not describe stubbed subsystems as working, and do not treat `README.md`/`docs/*.md` as
  a description of current behaviour.
- Missing components are findings — surface them rather than inventing an implementation.
- Prefer completing one vertical slice end-to-end over widening the scaffold.
