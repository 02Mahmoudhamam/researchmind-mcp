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
`python-jose`, `passlib`, `structlog`, `httpx`.
Frontend: Next.js 14 (App Router), React 18, TypeScript, Tailwind, axios, react-query,
zustand, recharts.

**Declared but never imported:** `anthropic`, `pymupdf`/`fitz`, `langchain-text-splitters`,
`passlib`, `python-multipart` — these map exactly to the five stubbed subsystems.
**Required but undeclared:** `openai` (the default embedding model is
`text-embedding-3-small`). `email-validator` was also missing and is now declared via
the `pydantic[email]` extra (M0/S0.1); before that, `shared/models/user.py` and
`backend/api/schemas/auth.py` could not be imported at all.

---

## Entry points

| Purpose | Command | File |
|---|---|---|
| Backend API | `python main.py` → Uvicorn on `APP_PORT` (8000) | [main.py](main.py) |
| FastAPI app object | `backend.api.app:app` | [backend/api/app.py](backend/api/app.py) |
| MCP server (stdio) | `python -m mcp_server.server.server` | [mcp_server/server/server.py](mcp_server/server/server.py) |
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
| Domain models | [shared/models/](shared/models/) | **Complete** — best asset in the repo |
| Interfaces (ABCs) | [shared/interfaces/](shared/interfaces/) | **Complete** — `BaseAgent`, `BaseVectorStore`, `BaseMemoryStore`, `BaseRepository` |
| Settings | [backend/config/settings.py](backend/config/settings.py) | **Complete** (insecure secret defaults); validates `DATABASE_URL` uses the asyncpg driver |
| DB infrastructure | [backend/db/](backend/db/) | **Complete (M1/S1.1)** — `Base` + naming convention, lazy async engine, session factory. **No models, no migrations** |
| API routers | [backend/api/routers/](backend/api/routers/) | Signatures only, all bodies `TODO` |
| API schemas | [backend/api/schemas/](backend/api/schemas/) | **Complete** |
| Services | [backend/services/](backend/services/) | Stubs, except `AgentService.run()` |
| Security | [backend/security/](backend/security/) | **All stubs** — JWT, RBAC, `get_current_user` |
| RAG pipeline | [document_processing/](document_processing/) | **All stubs** |
| Vector store | [vector_db/qdrant/](vector_db/qdrant/) | Client + config complete; repository all stubs |
| Memory | [memory_system/redis/](memory_system/redis/) | Client + config complete; store stubbed **and orphaned** |
| Agents (×9) | [agents/](agents/) | Identical templates; prompts + configs complete, `run()` is a stub |
| MCP tools/resources/prompts | [mcp_server/](mcp_server/) | Importable since M0/S0.2; `list_tools` returns all 7 schemas. Handlers still stubs (M6) |
| Logging | [shared/utils/logger.py](shared/utils/logger.py) | Complete, invoked — but `get_logger()` is never called |
| Health/metrics | [devops/monitoring/health.py](devops/monitoring/health.py) | **Router never mounted** |

### Orphaned code — defined, zero references anywhere
`RedisMemoryStore`, `require_role`, the health router,
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
- **No ORM models, migrations or repositories** — *narrowed by M1/S1.1.* PostgreSQL 16 is
  now a compose service with a healthcheck, `DATABASE_URL` is a `Settings` field, and
  `backend/db/` provides the declarative `Base`, a lazily-built async engine and a session
  factory; `backend/api/dependencies/database.py` yields `get_db_session` alongside Qdrant
  and Redis. **There is still no schema:** `Base.metadata.tables` is empty and asserted so
  by test. `User` and `Document` therefore still have nowhere to persist and
  `BaseRepository` still has no implementations — tables and the first migration are
  **S1.2**, repositories **S1.3**.
- **No task queue.** Ingest runs inline in the request handler.
- **No reranker, no hybrid/BM25 search, no query expansion.**

---

## RAG parameters (where they actually live)

| Stage | Value | Location | Note |
|---|---|---|---|
| Chunking | `chunk_size=512`, `chunk_overlap=64` | `document_processing/chunker.py:10` | Hard-coded; **units unspecified**; not in `Settings` or `.env` |
| Embeddings | `text-embedding-3-small` (1536-d) | `document_processing/embedder.py:9` | OpenAI model; `openai` not a dependency; no `OPENAI_API_KEY` anywhere |
| Vector store | Qdrant, Cosine, `vector_size=1536` | `vector_db/qdrant/config.py:12-13` | Dimension hard-coded to match the embedding model |
| Retrieval | `limit=10`, `score_threshold=0.7` | `backend/api/schemas/search.py:9-10` | Client-controllable; dense-only |
| Generation | `claude-sonnet-4-20250514`, `max_tokens=4096`, `temp=0.3` | `agents/*/config.py` | Identical across all 9 agents, including the Router |

`ensure_collection_exists()` is a stub and is never called — first run against a fresh
Qdrant will fail.

---

## Conventions

- **Layout:** one directory per bounded context; agents follow a strict 4-file pattern —
  `interface.py` (ABC subclass with `name`/`description`), `config.py` (an `AgentConfig`),
  `prompt.py` (`SYSTEM_PROMPT` + `build_prompt`), `service.py` (the concrete class).
- **Async everywhere** for I/O (`AsyncQdrantClient`, `redis.asyncio`, `async def` routes).
  Caution: `pymupdf` is a sync C-extension — calling it inside `async def parse()` without
  a thread offload will block the event loop.
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
  **`mypy --strict` is the exception** — configured and runnable (S0.5 fixed the
  module-path defect that made it abort before checking anything), but **not enforced**:
  it reports **149 errors in 66 files, 54 of them `empty-body`** — the stubs that declare
  a non-`Optional` return and then `...`. Promoted at **M2**, per the schedule in that
  workflow's header.

---

## Top blockers (P0 — full list in the audit)

1. ~~**`mcp/` shadows the `mcp` SDK**~~ — **RESOLVED in M0/S0.2.** Renamed to `mcp_server/`.
2. ~~**`pyproject.toml:6`**~~ — **RESOLVED in M0/S0.1.**
3. **No system of record** — *partially addressed.* The PostgreSQL connection exists as of
   M1/S1.1, but nothing is stored yet: no tables, no repositories. Still blocks auth,
   documents, ownership and multi-tenancy until S1.2–S1.3 land.
4. **Auth fails open** — `HTTPBearer` rejects a *missing* header (so it looks correct), but
   `get_current_user` verifies nothing: any non-empty Bearer string is accepted.
5. **Embedding provider unresolved** — OpenAI default in an Anthropic-only project.
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
