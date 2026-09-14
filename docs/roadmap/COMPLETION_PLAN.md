# ResearchMind MCP — Completion Plan

**Date:** 2026-09-08 · **Status:** Proposed, pending approval of [DECISIONS.md](../adr/0000-original-decision-record.md)
**Companions:** [PROJECT_VISION.md](PROJECT_VISION.md) · [ARCHITECTURE_AUDIT.md](../architecture/AUDIT-2026-09.md)

> **This plan assumes the five `DEFAULT` decisions in [DECISIONS.md](../adr/0000-original-decision-record.md).** Each ADR
> carries an *"If you override"* block; every task below that would change is tagged
> **`⟲ ADR-00n`**. Nothing here is implemented — this is a plan awaiting approval.

**Guiding principle: one tool working end-to-end beats seven working partially.** The repo's
current failure mode is breadth without depth — `[FACT — audit §1.4]` 152 files, 92 `TODO`s, 99
stub bodies, and not one path that executes. Every phase below is ordered so that the system is
*more runnable* at its end than at its start.

Effort scale: **S** ≈ hours · **M** ≈ 1–3 days · **L** ≈ 1–2 weeks (solo, focused).

| Phase | Goal | Effort | Cumulative state |
|---|---|---|---|
| [0](#phase-0) | Unblock: VCS, licence, build | **M** | `docker compose up` works |
| [1](#phase-1) | System of record | **M** | Data persists; ownership *checkable* |
| [2](#phase-2) | Auth done correctly | **M** | Ownership *enforced* |
| [3](#phase-3) | Vertical RAG slice | **L** | 📸 **SHOWABLE** — a real question gets a cited answer |
| [4](#phase-4) | Evaluation harness | **M** | Quality is measurable |
| [5](#phase-5) | Production hardening | **L** | 🏭 **"Production-grade" claim earned** |
| [6](#phase-6) | Breadth & OSS polish | **L** | Feature-complete, contributor-ready |

---
<a name="phase-0"></a>
## Phase 0 — Foundations & unblock  **[S/M]**

**Goal.** Make the repository buildable, runnable and version-controlled. Nothing downstream can
be *verified* until `docker compose up -d` succeeds, so this phase is a hard prerequisite for
everything.

**In scope:** version control, licensing, build correctness, the package-shadowing fix, frontend
build config, container hygiene.
**Out of scope:** any behaviour. No stub gets a body in Phase 0.

### Tasks

| # | Task | File(s) | Why |
|---|---|---|---|
| 0.1 | `git init`; initial commit of the current tree **before** any change, so the audit baseline is a real commit | — | `[FACT — verified this session]` `git status` → *"not a git repository"*. There is no history at all. |
| 0.2 | Add `.gitignore`: `.env`, `__pycache__/`, `.venv/`, `node_modules/`, `.next/`, `uploads/`, `.vscode/`, `*.db`, `*.db-wal`, `*.db-shm` | `.gitignore` | `[FACT — audit §4.7]` Absent. `.vscode/browse.vc.db*` artefacts are already sitting in the tree. |
| 0.3 | Add `LICENSE` — **MIT** recommended for a portfolio project (Apache-2.0 if a patent grant is wanted) | `LICENSE` | `[FACT — audit P0-9]` Without one, default copyright applies: no one may legally use, fork or contribute. |
| 0.4 | **Fix `pyproject.toml:6`** — remove `python = "^3.11"` from `[tool.poetry]` (it is already correct at line 9); add `package-mode = false` | `pyproject.toml` | `[FACT — verified]` `poetry check` → *"Additional properties are not allowed ('python' was unexpected)"*. `package-mode = false` also resolves the missing-root-package problem `[FACT — audit §4.5]`, since this is a flat multi-package app, not a library. |
| 0.5 | Add missing deps: `email-validator` (or `pydantic[email]`) | `pyproject.toml` | `[FACT — audit §1.2]` `EmailStr` is used in 3 models; the package is undeclared and would fail at class-definition time. |
| 0.6 | `poetry lock`; commit `poetry.lock` | `poetry.lock` | `[FACT — audit §4.5]` Absent, yet `Dockerfile.backend:8` copies it. Builds are currently irreproducible across 15 `^`-ranged packages. |
| 0.7 | **Rename `mcp/` → `mcp_server/`**; update every intra-package import and `Dockerfile.mcp:10`'s `CMD` | `mcp/` → `mcp_server/`, `devops/docker/Dockerfile.mcp` | `[FACT — verified]` `import mcp` resolves to `./mcp/__init__.py` and `mcp.server` has **no `Server` attribute**. The SDK is unreachable; the headline feature cannot import. |
| 0.8 | Delete `mcp/client/` | — | **⟲ ADR-001.** `[FACT — audit §1.5]` Stubbed, never instantiated; the backend will not speak MCP. |
| 0.9 | Pin Poetry in both Dockerfiles (`pip install poetry==1.8.5`) and replace `--no-dev` with `--only main` | `devops/docker/Dockerfile.{backend,mcp}` | `[FACT — audit §4.5]` `pip install poetry` is unpinned → installs 2.x, where `--no-dev` **was removed**. A second, independent build break. |
| 0.10 | Add `.dockerignore` (`.env`, `.git`, `node_modules`, `.vscode`, `__pycache__`) | `.dockerignore` | `[FACT — audit §4.5]` `COPY . .` would otherwise bake `.env` into an image layer. |
| 0.11 | Make both Python images multi-stage and add a non-root `USER` | `devops/docker/Dockerfile.{backend,mcp}` | `[FACT — audit §4.5]` Both run as root; `build-essential` ships in the runtime layer. |
| 0.12 | Add `frontend/next.config.js` with `output: 'standalone'`, plus `tailwind.config.js` and `postcss.config.js` | `frontend/` | `[FACT — verified absent]` `Dockerfile.frontend:11` copies `.next/standalone`, which Next.js only emits when configured. Tailwind directives in `globals.css:1-3` currently compile to nothing. |
| 0.13 | Mount the health router | `backend/api/app.py`, `devops/monitoring/health.py` | `[FACT — audit §4.3]` Never mounted; `grep "health" backend/` returns nothing, which is why `tests/integration/test_api.py:10` fails. |
| 0.14 | Compose hygiene: drop obsolete `version:`, add `restart: unless-stopped`, add `healthcheck:` + `depends_on: condition: service_healthy`, create `uploads/` with `.gitkeep`, bind Qdrant/Redis to `127.0.0.1` | `docker-compose.yml` | `[FACT — audit §4.5]` `depends_on` orders start, not readiness — the backend races Qdrant on boot. Qdrant/Redis are currently exposed on all interfaces without auth. |
| 0.15 | Remove the `mcp-server` service from compose; document the Claude Desktop stdio config in the README instead | `docker-compose.yml`, `README.md` | **⟲ ADR-001.** `[FACT — audit §3.4]` A stdio server as an always-on container with nothing on stdin is inert. |

**Dependencies:** none. This phase is the root of the graph.

**Risks.** `[INFERENCE]` Task 0.7 is a wide mechanical rename touching ~20 files; do it as an
isolated commit so a `git bisect` can cleanly separate it from behavioural work. Low risk, but
noisy in review.

### Definition of Done — verifiable

```bash
git log --oneline | tail -1          # → an initial commit exists
poetry check                          # → "All set!" (exit 0)
docker compose build                  # → all images build
docker compose up -d && sleep 15
curl -fsS localhost:8000/health       # → {"status":"ok"}
python3 -c "import mcp, mcp.server; print(mcp.server.Server)"   # → resolves to site-packages, not ./mcp
```
Plus: `.env` appears in `.gitignore` **and** `.dockerignore`; `LICENSE` exists at the root.

---
<a name="phase-1"></a>
## Phase 1 — System of record  **[M]**

**Goal.** Give `User` and `Document` a real home, so ownership becomes a server-side fact rather
than a client-supplied claim. `[FACT — audit Chain B]` This is the audit's largest risk.

**In scope:** Postgres, ORM, migrations, repositories, ownership queries.
**Out of scope:** auth (Phase 2), any RAG behaviour (Phase 3).

### Tasks

| # | Task | File(s) |
|---|---|---|
| 1.1 | Add `postgres:16` to compose with a named volume and a healthcheck | `docker-compose.yml` |
| 1.2 | Add `sqlalchemy[asyncio]`, `asyncpg`, `alembic`; add `DATABASE_URL` to `Settings` + `.env.example` | `pyproject.toml`, `backend/config/settings.py`, `.env.example` |
| 1.3 | Create `backend/db/`: async engine, `async_sessionmaker`, declarative `Base` | `backend/db/{engine,base}.py` *(new)* |
| 1.4 | ORM models `UserORM`, `DocumentORM`, `DocumentChunkORM`, mapping to the **existing** Pydantic models — the Pydantic layer stays the API contract, ORM is persistence only | `backend/db/models.py` *(new)*, mapping `shared/models/{user,document}.py` |
| 1.5 | `alembic init`; first migration; `alembic upgrade head` wired into container start | `alembic/`, `alembic.ini` *(new)* |
| 1.6 | Implement `BaseRepository` as `UserRepository` and `DocumentRepository` — ownership-scoped queries take `user_id` as a **mandatory** argument, not an optional filter | `backend/repositories/*.py` *(new)*, implementing `shared/interfaces/repository.py:9-25` |
| 1.7 | Add `get_db_session` dependency | `backend/api/dependencies/database.py` (currently yields only Qdrant + Redis) |
| 1.8 | Implement `DocumentService.{get_document,list_user_documents,delete_document}` against the repositories | `backend/services/document_service.py:17-27` |
| 1.9 | Record `embedding_model_id`, `dimension` and `chunking_strategy_version` on `DocumentORM` | `backend/db/models.py` |

`[INFERENCE]` Task 1.9 looks premature but is not: **⟲ ADR-002/005** both note that changing the
embedding model or chunking strategy invalidates stored vectors. Recording these at ingest time is
what makes a targeted re-index possible later instead of a guess.

**Dependencies:** Phase 0 (a working build to run migrations against).

**Risks.** `[INFERENCE]` The Pydantic-vs-ORM duplication is a real maintenance cost; keeping ORM
strictly at the persistence boundary (never returned from a service) is the discipline that keeps
it manageable. `[INFERENCE]` `⟲ ADR-004` — if pgvector is chosen instead, this phase absorbs the
vector store too and grows M→L, while Phase 3 shrinks.

### Definition of Done — verifiable

An integration test (`tests/integration/test_repositories.py`) that, against a real Postgres in compose:
1. creates User A and User B;
2. creates a Document owned by A;
3. asserts `DocumentRepository.get(doc_id, user_id=A)` returns it;
4. asserts `DocumentRepository.get(doc_id, user_id=B)` returns **`None`** — ownership is enforced in the query, not by a caller-side `if`;
5. asserts `alembic downgrade base && alembic upgrade head` round-trips cleanly.

---
<a name="phase-2"></a>
## Phase 2 — Auth done correctly  **[M]**

**Goal.** Replace a stub that **fails open** with one that fails closed.
`[FACT — audit §4.7]` `get_current_user` returns `None` and verifies nothing; `HTTPBearer` rejects a
*missing* header, so casual testing looks correct while **any non-empty string is accepted as a token**.

**In scope:** JWT lifecycle, password hashing, `get_current_user`, RBAC enforcement, CORS.
**Out of scope:** SSO/SAML, refresh-token rotation, OAuth (`[FACT — docs/ROADMAP.md:25]` Phase 4 aspirations; see vision §4.4).

### Tasks

| # | Task | File(s) |
|---|---|---|
| 2.1 | Replace `python-jose` with `pyjwt` | `pyproject.toml`, `backend/security/jwt_handler.py:4` |
| 2.2 | Implement `create_access_token` / `verify_token` — verify signature **and** `exp`, raise on any failure | `backend/security/jwt_handler.py:14-30` |
| 2.3 | ~~Password hashing with `passlib[bcrypt]` or `argon2-cffi` in `AuthService.register/login`~~ — **done, M2/S2.3**, with two deviations: `bcrypt` used **directly** (passlib picks a backend at import and can fall through to stdlib `crypt`, removed in 3.13), and the primitives live in `backend/security/passwords.py` rather than in `AuthService`, so registration *and* login share one policy. Storage only at the time; `AuthService` consumes it as of S2.4. | `backend/security/passwords.py`, `backend/db/models/user.py`, `alembic/versions/…_0002_…` |
| 2.4 | Implement `get_current_user`: verify token → load user from Postgres → 401 on *any* failure. **No path may return `None`.** | `backend/security/api_security.py:13-17` |
| 2.5 | ~~Implement `RBACPolicy.has_permission` / `get_permissions` and apply `require_role` to routes~~ — **done, M2/S2.5**, with one deviation: routes are authorised with **`require_permission`**, not `require_role`. The scaffold's matrix is permission-based, and a role list at each route would copy it into nine routers. `require_role` is implemented for role-only checks; no route needs one yet. `DocumentService` now takes a `Principal`. | `backend/security/rbac.py`, `backend/security/api_security.py`, `backend/api/routers/*`, `backend/services/document_service.py` |
| 2.6 | **Remove the `"changeme"` defaults** from `SECRET_KEY` and `JWT_SECRET`; add a validator rejecting known-weak values so the app refuses to boot | `backend/config/settings.py:15,37` |
| 2.7 | Replace CORS `allow_origins=["*"]` with `settings.CORS_ORIGINS` | `backend/api/app.py:17-22` |
| 2.8 | ~~Implement `POST /auth/{register,login}` end-to-end~~ — **done, M2/S2.4.** Register returns **201 and the account, not a token**; login issues the S2.1 token. Uniqueness is enforced by `uq_users_email`, not a pre-check, so concurrent registrations cannot both succeed. | `backend/api/routers/auth.py`, `backend/services/auth_service.py`, `shared/models/credentials.py` |

`[INFERENCE]` Task 2.1 is not gold-plating: `python-jose` `[FACT — pyproject.toml:20]` has had no
release since 2021 and carries known algorithm-confusion CVEs. Swapping it while the module is
still a stub costs nothing; swapping it later costs a migration.

**Dependencies:** Phase 1 (users must persist before tokens can bind to them).

**Risks.** `[INFERENCE]` Task 2.6 will break any developer's existing `.env`; call it out in the
README. That is the intended behaviour — a system that boots with a known signing key is worse
than one that refuses to boot.

### Definition of Done — verifiable

`tests/integration/test_auth.py` asserting all four:
| Request | Expected |
|---|---|
| No `Authorization` header | **401** |
| `Authorization: Bearer garbage` | **401** ← *the case that passes today* |
| Valid token | **200**, and `user.id` matches the token subject |
| User A's token requesting User B's document | **404** |

Plus: the app **refuses to start** when `JWT_SECRET` is unset or `"changeme"`.

---
<a name="phase-3"></a>
## Phase 3 — Vertical RAG slice  📸 **SHOWABLE MILESTONE**  **[L]**

**Goal.** One document type (PDF), one tool (`semantic_search`), one agent — **working end to
end**, reachable both from the REST API and from Claude Desktop over MCP.

**In scope:** parse → chunk → embed → owner-scoped upsert → retrieve → validate → generate, plus
one MCP tool over stdio and async ingest.
**Explicitly out of scope:** the other six tools, knowledge graph, comparison, workspaces, session
memory, frontend polish, reranking, hybrid search. **Resist all of it.**

### Tasks

| # | Task | File(s) | Notes |
|---|---|---|---|
| 3.1 | `EmbeddingProvider` protocol with `embed_documents`, `embed_query`, and `dimension` / `model_id` **properties** | `shared/interfaces/embedding.py` *(new)* | **⟲ ADR-002** |
| 3.2 | `FastEmbedProvider` (`BAAI/bge-small-en-v1.5`, 384-d); bake the model into the image for offline start | `document_processing/embedder.py:6-19` | **⟲ ADR-002** |
| 3.3 | **Derive Qdrant `vector_size` from the provider** — remove the hard-coded `1536` | `vector_db/qdrant/config.py:12` | `[FACT]` Hard-coded today; a model swap would fail at runtime, not startup |
| 3.4 | Implement `ensure_collection_exists`; call it from a FastAPI `lifespan` handler | `vector_db/qdrant/client.py:15-17`, `backend/api/app.py` | `[FACT — audit §4.2]` Guaranteed first-run failure today; `[FACT — audit §4.8]` no lifespan handler exists |
| 3.5 | PDF parsing: PyMuPDF **block mode with column-aware ordering**, via `anyio.to_thread.run_sync` | `document_processing/pdf_parser.py:10-20` | **⟲ ADR-005.** `[INFERENCE]` PyMuPDF is a sync C extension; calling it in `async def` blocks the whole event loop (audit Chain C) |
| 3.6 | Section detection + token-based chunking within sections; references split on entry boundaries; **fallback to fixed windows when no sections are detected** | `document_processing/chunker.py:14-19` | **⟲ ADR-005** |
| 3.7 | Move `CHUNK_SIZE_TOKENS`, `CHUNK_OVERLAP_TOKENS`, `RETRIEVAL_TOP_K`, `RETRIEVAL_SCORE_THRESHOLD` into `Settings` | `backend/config/settings.py`, `.env.example` | `[FACT — audit §4.1]` Hard-coded today; Phase 4 cannot tune what Phase 3 fixes in code |
| 3.8 | `upsert` with payload `{document_id, user_id, chunk_index, section, content}`; **create keyword payload indexes on `user_id` and `document_id`** | `vector_db/qdrant/repository.py:17-19` | **⟲ ADR-004.** `[FACT — audit §4.8]` No payload index exists → filtering degrades to a scan |
| 3.9 | `search` with a **mandatory** `must` filter on `user_id` | `vector_db/qdrant/repository.py:21-29` | **⟲ ADR-004** |
| 3.10 | Wire `DocumentProcessingPipeline.process` end-to-end and set `DocumentStatus` transitions | `document_processing/pipeline.py:24-30` | `[FACT — shared/models/document.py:15-19]` The status enum already models this |
| 3.11 | **Async ingest** via ARQ: add the dep, a `worker` service in compose, enqueue from `DocumentService.upload_and_process` | `backend/services/document_service.py:13-15`, `worker/` *(new)*, `docker-compose.yml` | `[INFERENCE]` Fixes audit Chain C and makes `PROCESSING` reachable. Redis is `[FACT — audit §1.5]` already provisioned and used for nothing. |
| 3.12 | Upload validation: size cap, magic-byte MIME check, filename sanitisation | `backend/api/routers/documents.py:11-18` | `[FACT — audit §4.7]` None today, against a bind-mounted upload dir |
| 3.13 | `SearchService.search`: embed → Qdrant (filtered) → **validate returned `document_id`s against Postgres** → return | `backend/services/search_service.py:13-21` | **⟲ ADR-004 invariant** — the second layer of ownership defence |
| 3.14 | Single `ResearchAgent`: Anthropic client, tool-use loop, **real token budgeting**, prompt caching on the static system prompt, retry/backoff reading `AgentConfig.retry_attempts` | `agents/research/` *(new)*; delete the 8 unused packages | **⟲ ADR-003.** `[FACT]` `retry_attempts` is declared and never read |
| 3.15 | **Close the RAG loop:** `AgentService` populates `AgentInput.context` from `SearchService`; fence untrusted document text in delimiters | `backend/services/agent_service.py:14-19` | `[FACT — audit §2.2]` This edge **does not exist today, even in stub form** — retrieval and generation are unconnected |
| 3.16 | Fix the `semantic_search` tool schema — add `query`, `limit`, `document_ids` | `mcp_server/tools/semantic_search.py:7-17` | `[FACT]` The current schema has **no `query` field**; `docs/MCP.md:10` contradicts the code |
| 3.17 | Implement `call_tool` dispatch, `list_resources`, `read_resource`, `list_prompts`, `get_prompt` — tool handlers call `SearchService` **directly**, not over HTTP | `mcp_server/server.py:47-74` | **⟲ ADR-001.** `[FACT]` Five of six handlers are stubs |
| 3.18 | Parent-document retrieval: return the enclosing section, looked up from `DocumentChunkORM` | `backend/services/search_service.py` | **⟲ ADR-005** |

**Dependencies:** Phases 0, 1, 2. Task 3.13 depends on 1.6; 3.9 depends on 2.4.

**Risks.**
`[INFERENCE]` **Scope creep is the dominant risk of this phase** — every task here has an obvious
"while I'm in here" neighbour. The out-of-scope list above is the mitigation; treat it as binding.
`[INFERENCE]` Section-heading detection will fail on unusual layouts; task 3.6's fallback is what
keeps that a degradation rather than a corruption.
`[INFERENCE]` No quality bar can be *asserted* in this phase, because Phase 4 does not exist yet.
Phase 3's DoD is deliberately about **correctness and reachability**, not answer quality — resist
the urge to tune retrieval here, since it cannot yet be measured.

### Definition of Done — verifiable

A scripted end-to-end test (`tests/e2e/test_vertical_slice.py`) against `docker compose up`:
1. Register a user, obtain a token.
2. `POST /api/v1/documents/upload` with a **real** PDF fixture → `202` and a document id.
3. Poll `GET /api/v1/documents/{id}` until `status == "ready"` (bounded timeout).
4. `POST /api/v1/search` with a question whose answer is in that PDF → ≥1 result, each with a
   score, a `section`, and `document_id` owned by the caller.
5. A **second user** issuing the identical search → **zero results**.
6. `POST /api/v1/agents/run` → a grounded answer that cites at least one retrieved chunk id.
7. **Manual, and the actual demo:** Claude Desktop configured with the stdio server calls
   `semantic_search` and returns the same passages.

> 📸 **This is the showable line.** At the end of Phase 3 the project does something real, on a
> real document, through both of its intended interfaces. Everything before it is scaffolding;
> everything after it is rigour and breadth.

---
<a name="phase-4"></a>
## Phase 4 — RAG evaluation harness  **[M]**

**Goal.** Make retrieval and answer quality **measurable**, so every later tuning decision is
evidence-based. `[FACT — audit §4.4]` No evaluation of any kind exists today.

`[INFERENCE]` **This phase must precede all quality tuning.** Adjusting `top_k`, chunk size, or
adding a reranker without a baseline is guesswork that *feels* like engineering. It is placed
immediately after the slice so that Phase 5 and 6 changes can be regression-checked.

**In scope:** golden set, retrieval metrics, answer metrics, baseline, CI regression gate.
**Out of scope:** acting on the results — tuning happens in Phase 6, informed by this.

### Tasks

| # | Task | File(s) |
|---|---|---|
| 4.1 | Fixture corpus: ~10 open-access papers (arXiv/PMC) fetched by script, not committed as blobs | `eval/corpus/fetch.py` *(new)* |
| 4.2 | Golden set: 30–50 `(question, relevant_chunk_ids, reference_answer)` triples, including deliberate negatives (questions the corpus **cannot** answer) | `eval/golden_set.yaml` *(new)* |
| 4.3 | Retrieval metrics: recall@k, MRR, nDCG@10, hit-rate — **plus tokens-retrieved**, since ADR-005 makes chunk size variable | `eval/metrics/retrieval.py` *(new)* |
| 4.4 | Answer metrics: groundedness/faithfulness via LLM-as-judge (Claude), citation accuracy, and refusal-correctness on the negatives | `eval/metrics/answer.py` *(new)* |
| 4.5 | Runner producing a table + a JSON artefact per run | `eval/run.py` *(new)* |
| 4.6 | Record the Phase-3 baseline as the reference point | `eval/results/baseline.json` |
| 4.7 | CI gate: fail if recall@5 or groundedness regresses beyond a threshold | `.github/workflows/eval.yml` |

**Dependencies:** Phase 3 (there must be something to evaluate).

**Risks.** `[INFERENCE]` LLM-as-judge is itself non-deterministic and costs money per run — pin the
judge model, set `temperature=0`, and keep the golden set small enough to run in CI affordably.
`[INFERENCE]` A golden set authored by the same person who wrote the retrieval code will encode
that person's assumptions; the negative examples in 4.2 are the main guard against a set that only
proves what the system already does.

### Definition of Done — verifiable

```bash
poetry run python -m eval.run          # → metrics table + eval/results/<date>.json
```
And a **falsifiability check**: setting `RETRIEVAL_TOP_K=1` produces a *measurably* lower recall@5
than the baseline. `[INFERENCE]` A harness that cannot detect a deliberate regression is not
measuring anything — this check is the one that proves the harness works.

---
<a name="phase-5"></a>
## Phase 5 — Production hardening  🏭  **[L]**

**Goal.** Earn the word "production-grade" that `[FACT — README.md:3]` the README already claims.

**In scope:** failure modes, observability, config/secrets, CI, rate limiting, injection defence.
**Out of scope:** new features.

### 5a — Failure modes
| # | Task | File(s) |
|---|---|---|
| 5.1 | Global exception handler — **never** return raw exception strings to clients | `backend/api/app.py`; fixes `agents/*/service.py:37` leak `[FACT — audit §4.2]` |
| 5.2 | Retry + exponential backoff on Anthropic 429/529, honouring `AgentConfig.retry_attempts` | `agents/research/service.py` |
| 5.3 | Timeouts and a circuit breaker around Qdrant and Postgres; **503, not 500**, when a dependency is down | `vector_db/qdrant/repository.py`, `backend/db/` |
| 5.4 | Implement `/health/ready` to actually check Postgres, Qdrant, Redis and Anthropic reachability | `devops/monitoring/health.py:13-17` |
| 5.5 | Idempotent ingest + reconciliation for orphaned vectors (ADR-004 deletion order) | `worker/`, `backend/services/document_service.py` |

### 5b — Observability
| # | Task | File(s) |
|---|---|---|
| 5.6 | **Actually call `get_logger()`** — it is defined and used nowhere `[FACT — audit §4.3]` | throughout |
| 5.7 | Request-ID middleware; bind it into every structlog context | `backend/api/app.py` |
| 5.8 | Log the RAG-specific facts: retrieved chunk ids, scores, tokens in/out, per-stage latency | `backend/services/search_service.py`, `agents/research/` |
| 5.9 | Assign `AgentOutput.tokens_used`; persist `latency_ms` instead of computing and discarding it | `agents/research/service.py`; `[FACT — shared/models/agent.py:22-23]` |
| 5.10 | `prometheus_client` at `/metrics` | `devops/monitoring/health.py:20-24` |
| 5.11 | Delete the duplicate `devops/logging/logging_config.py` | — `[FACT — audit §4.3]` near-duplicate of `shared/utils/logger.py`, never imported |

### 5c — Config, security, CI
| # | Task | File(s) |
|---|---|---|
| 5.12 | Fix import-time config binding (`default_factory`) so tests can override settings | `vector_db/qdrant/config.py:8-14`, `memory_system/redis/config.py:8-13` `[FACT — audit §4.1]` |
| 5.13 | Rate limiting (Redis token bucket) | `backend/api/app.py` — `[INFERENCE]` each agent call spends money; this is denial-of-**wallet**, not just load |
| 5.14 | Prompt-injection defence: fence untrusted document text, separate system/user turns, instruct that document content is data | `agents/research/prompt.py` `[FACT — audit §4.7]` raw f-string interpolation today |
| 5.15 | Postgres/Qdrant/Redis auth; no `0.0.0.0` binds | `docker-compose.yml` |
| 5.16 | **CI**: ruff, `black --check`, **`mypy --strict`**, pytest with services, docker build, `pip-audit` | `.github/workflows/ci.yml` |

`[INFERENCE]` Task 5.16 will be the loudest: `[FACT — pyproject.toml:41-43]` `mypy strict = true` is
already configured and has never run. Expect real work to make it pass — though Phase 3 will have
replaced most of the ~99 stub returns that would fail it today.

**Dependencies:** Phases 3, 4 (CI should gate on the eval harness).

### Definition of Done — verifiable
1. CI green on a pull request, including `mypy --strict`.
2. `docker compose stop qdrant` → `/health/ready` returns **503**, and `POST /api/v1/search`
   returns a clean **503** with no stack trace and no internal hostnames.
3. A request log line contains a request id, retrieved chunk ids, scores and token counts.
4. `/metrics` serves Prometheus-format output.
5. The app refuses to boot with a weak `JWT_SECRET` (from Phase 2), verified in CI.

---
<a name="phase-6"></a>
## Phase 6 — Breadth & OSS polish  **[L]**

**Goal.** Widen from one tool to seven, and make the repository honest and contributable.

| # | Task | File(s) |
|---|---|---|
| 6.1 | Implement the remaining six tools, **each with a genuinely distinct input schema** | `mcp_server/tools/*` — `[FACT — audit §2.1]` all seven are currently identical |
| 6.2 | Stage the hard two last: `detect_research_gaps`, `build_knowledge_graph` (vision §4.5) | `mcp_server/tools/` |
| 6.3 | MCP **resource templates** (`research://paper/{id}`) replacing static URIs; correct mimeTypes | `mcp_server/resources/*` — `[FACT]` `pdf_resource` declares `application/json` |
| 6.4 | Implement the five remaining prompts | `mcp_server/prompts/*` |
| 6.5 | Session memory: finally wire `RedisMemoryStore` | `memory_system/redis/store.py` — `[FACT — audit §1.5]` orphaned since day one |
| 6.6 | **Rewrite `README.md`** — present tense **only** for what works, plus a Status table and Claude Desktop setup | `README.md` |
| 6.7 | Rewrite `docs/AGENTS.md` (describes 9 agents that no longer exist) and `docs/MCP.md` (contradicts the code); write the missing `docs/API.md`; fill the empty `docs/diagrams/` | `docs/` |
| 6.8 | `CONTRIBUTING.md`, code of conduct, issue/PR templates | root |
| 6.9 | Wire the six frontend pages to the API | `frontend/src/app/*` — currently placeholder headings |
| 6.10 | **OPTIONAL**, gated on Phase 4 evidence: reranking, hybrid/BM25 search, streaming responses, GROBID parsing, Streamable-HTTP MCP transport (**⟲ ADR-001**), pgvector migration (**⟲ ADR-004**) | — |

**Dependencies:** Phases 3–5.

**Risks.** `[INFERENCE]` Task 6.6 is the one with reputational weight. `[FACT — audit §4.9]` The
current docs describe a working system that has never run; a contributor who follows the Quick
Start hits a `poetry check` failure. Fixing that gap matters more for an OSS portfolio project than
any individual feature in this phase.

### Definition of Done — verifiable
1. All seven MCP tools callable from Claude Desktop with distinct, meaningful schemas.
2. Every claim in `README.md` is demonstrable by a command in the README itself.
3. No broken internal doc links (`docs/API.md` exists; `docs/diagrams/` is non-empty).
4. A fresh clone → `docker compose up` → working system, following only the README.

---

## Milestones

| Line | Phases | Claim it earns |
|---|---|---|
| 📸 **Showable** | **0 → 3** | *"Upload a paper, ask a question, get a cited answer — in the app and inside Claude Desktop."* Demoable, honest, and genuinely differentiated. |
| 🏭 **Production-grade** | **0 → 5** | The README's existing claim becomes true: measurable quality, real failure handling, observability, enforced CI. |
| 🎁 **Feature-complete** | **0 → 6** | All seven tools, honest docs, contributor-ready. |

`[INFERENCE]` **Phases 0–3 are the minimum credible portfolio milestone.** A project that does one
thing completely — with real auth, real ownership, and a real MCP integration — is a stronger
signal than one advertising seven capabilities that all return `"Not yet implemented"`
`[FACT — mcp/tools/semantic_search.py:23]`.

`[INFERENCE]` If time is constrained, the highest-value cut is **Phase 6, not Phase 4**. Skipping
evaluation to ship breadth is how a RAG project becomes unimprovable: without a baseline, no one
can tell whether the seventh tool made the system better or worse.

## Sequencing rationale

`[INFERENCE]` Three ordering constraints are non-negotiable, each from a specific audit finding:

1. **Phase 1 before Phase 2** — tokens must bind to persisted users, or auth is decorative (Chain B).
2. **Phase 2 before Phase 3** — the Qdrant `user_id` filter needs a *trustworthy* `user_id`. Building
   retrieval on an unauthenticated identity means rebuilding it.
3. **Phase 4 before any tuning in Phase 6** — chunking and `top_k` changes are unfalsifiable without
   a baseline, and `[FACT — ADR-005]` re-chunking forces a full re-embed, so guessing is expensive.

Everything else can be resequenced if priorities change.
