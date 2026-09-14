# ResearchMind MCP

An AI research assistant for working with a personal corpus of academic papers,
exposed both as a REST API and as a Model Context Protocol (MCP) server.

> [!IMPORTANT]
> **Development status: pre-alpha. The system does not yet run.**
> This repository is an architectural foundation with an approved delivery plan.
> Most capabilities described below are **not implemented**. The
> [Development Status](#development-status) section is exact about what exists.
> Nothing here is production-ready or safe to expose to a network.

---

## What It Does

The goal is a research assistant for an individual academic or a small research
group. A researcher uploads papers, and the system makes their own corpus
answerable:

- **Semantic search** across everything they have uploaded.
- **Grounded answers** to research questions, drawn only from their documents
  and returned **with citations that resolve to a real page and section**.
- **Document-scoped analysis** — summarise a paper, compare several.

MCP is the integration seam. The same capabilities are reachable from the
project's own web client *and* from an external MCP host such as Claude
Desktop, because both are thin adapters over one service core.

**What it is not.** Not a literature search engine — it works on documents you
supply. Not multi-tenant SaaS. Not a chatbot with general knowledge: if the
answer is not in your corpus, the correct response is "no relevant sources",
and the system is built to say so rather than to improvise.

---

## Architecture

A **modular monolith**. REST and MCP are sibling adapters over a shared Service
Core; neither calls the other.

```
┌──────────────────┐            ┌────────────────────────┐
│  Next.js client  │            │  MCP host              │
│  (browser)       │            │  (e.g. Claude Desktop) │
└────────┬─────────┘            └───────────┬────────────┘
         │ HTTPS + Bearer JWT               │ stdio subprocess
┌────────▼─────────────────┐   ┌────────────▼─────────────┐
│  REST ADAPTER            │   │  MCP ADAPTER             │
│  backend/api/            │   │  mcp_server/             │
│  routers · schemas       │   │  tool registry+dispatch  │
│         └────────────────┼───┼──► one identity resolver │
└────────┬─────────────────┘   └────────────┬─────────────┘
         └───────────────┬──────────────────┘
┌────────────────────────▼───────────────────────────────┐
│  SERVICE CORE — backend/services/                      │
│  Auth · Document · Ingestion · Search · Research       │
│  every method takes an authenticated Principal         │
└──┬──────────┬───────────┬───────────┬──────────────┬───┘
   ▼          ▼           ▼           ▼              ▼
┌──────┐ ┌─────────┐ ┌────────┐ ┌──────────┐ ┌────────────┐
│Repos │ │ Object  │ │Embed   │ │ Vector   │ │ LLM        │
│      │ │ storage │ │Provider│ │ Index    │ │ Provider   │
└──┬───┘ └────┬────┘ └───┬────┘ └────┬─────┘ └─────┬──────┘
   ▼          ▼          ▼           ▼             ▼
PostgreSQL  volume    FastEmbed   Qdrant       Anthropic
(SOURCE OF  (content- (local,     (INDEX       (Claude)
 TRUTH)     addressed) 384-d)      ONLY)
                          ▲
                     ┌────┴─────┐
                     │  Redis   │ ARQ job queue + rate limits
                     └──────────┘
```

### Storage responsibilities

| Store | Owns | Never |
|---|---|---|
| **PostgreSQL** | Sole authority for what exists and who owns it | Vectors |
| **Qdrant** | An index: vectors + `user_id`/`document_id` payload | A source of truth |
| **Redis** | ARQ job queue, job status, rate-limit counters | Anything whose loss is unrecoverable |
| **Object storage** | Original uploaded bytes, content-addressed | Anything derivable |

> **Isolation invariant.** Retrieval filters on `user_id` in Qdrant (fast path)
> **and** re-validates every chunk against PostgreSQL ownership before any
> content reaches a prompt (correct path). If the two ever disagree, the system
> returns fewer results — never another user's document.

Rationale for every structural choice is in [docs/adr/](docs/adr/).

---

## Technology Stack

| Layer | Technology |
|---|---|
| **Language / runtime** | Python 3.12 · Poetry |
| **API** | FastAPI · Uvicorn · Pydantic v2 · pydantic-settings |
| **System of record** | PostgreSQL 16 · SQLAlchemy 2 (async) · asyncpg · Alembic |
| **Vector index** | Qdrant (cosine) |
| **Jobs & cache** | Redis 7 · ARQ |
| **LLM** | Anthropic Claude (`claude-sonnet-4`) via the `anthropic` SDK |
| **Embeddings** | FastEmbed · `BAAI/bge-small-en-v1.5` (384-d, local) |
| **Document parsing** | PyMuPDF (block mode, thread-offloaded) |
| **MCP** | `mcp` Python SDK, stdio transport |
| **Auth** | JWT (`PyJWT`, HS256 pinned at decode, 60-minute access tokens, no refresh) · bcrypt password hashing · fail-closed `get_current_user` |
| **Frontend** | Next.js 14 (App Router) · React 18 · TypeScript · Tailwind · TanStack Query · Zustand · axios |
| **Testing** | pytest · pytest-asyncio · testcontainers · httpx · gitleaks |
| **Quality** | ruff · black *(both enforced in CI)* · mypy strict *(enforced in CI over the M2 security surface; not yet repository-wide)* |
| **Infrastructure** | Docker · Docker Compose · GitHub Actions · Dependabot |

Embeddings run **locally**, so a full stack needs exactly one secret:
`ANTHROPIC_API_KEY`. See [ADR-0004](docs/adr/0004-local-fastembed-embeddings.md).

---

## Repository Structure

| Path | Contents |
|---|---|
| `backend/` | REST adapter (`api/`) and the **Service Core** (`services/`), config, security |
| `mcp_server/` | MCP adapter — server, tools, resources, prompts. Importable; handlers stubbed until M6 |
| `agents/` | Agent layer *(collapsed to a single `ResearchAgent` at M5)* |
| `shared/` | Domain models, interfaces (ABCs), utilities — the layer everything depends on |
| `document_processing/` | RAG ingestion: parse → chunk → embed |
| `vector_db/` | Qdrant adapter |
| `memory_system/` | Redis adapter |
| `frontend/` | Next.js web client |
| `tests/` | `unit/`, `integration/`, `e2e/` |
| `infra/` | Dockerfiles and infrastructure configuration |
| `scripts/` | Developer and CI helper scripts |
| `docs/` | [Architecture](docs/architecture/), [ADRs](docs/adr/), [roadmap](docs/roadmap/), [development](docs/development/), [security](docs/security/) |

---

## Development Status

**Verified by execution, not by assumption.** The full evidence base is in
[docs/architecture/AUDIT-2026-09.md](docs/architecture/AUDIT-2026-09.md).

### What genuinely works

| Component | State |
|---|---|
| Domain models (`shared/models/`) | ✅ Complete — 12 Pydantic v2 models |
| Interfaces (`shared/interfaces/`) | ✅ Complete — 4 ABCs |
| API schemas (`backend/api/schemas/`) | ✅ Complete — 9 DTOs |
| Settings (`backend/config/settings.py`) | ✅ Complete |
| FastAPI app construction | ✅ Builds and mounts routers |
| Repository foundation | ✅ Docs, ADRs, CI hygiene, workflow |

### What does not work

| Area | State |
|---|---|
| **REST endpoints** | ⚠️ Auth, `/health`, and document **upload / list / get / delete** work — an upload is validated, stored and recorded, but stays `pending` until the ingestion worker (M3/S3.2). Search, agents and workspace are authorised but answer **501** — they wait on M3–M5 |
| **Authentication & authorisation** | ✅ **M2 complete.** Register and log in over HTTP; a forged, expired or unresolvable token is **401** on every protected route; a role without the permission is **403**; another user's document is **404** whatever your role |
| **MCP layer** | ⚠️ Imports correctly and lists its 7 tools; no tool handler is implemented yet (M6) |
| **RAG pipeline** | ❌ 1 of 17 stages implemented |
| **Persistence** | ⚠️ Schema (migrations `0001`–`0003`), ownership-scoped repositories and `DocumentService`. Uploaded PDFs are stored content-addressed on a local volume (ADR-0008) and recorded as `pending`; parsing and chunking are later M3 sprints |
| **Agents** | ❌ Return `success=True` without calling an LLM |
| **Container builds** | ⚠️ Two images, not three — the MCP container was removed in M0/S0.2 (ADR-0002 settled on stdio). Both were made to build in M0/S0.4–S0.5; not re-verified since |
| **Test suite** | ✅ 448 passed, 2 xfailed, against real PostgreSQL in CI |

Roughly **10% complete** by the pre-M0 audit's count. That figure has not been
re-measured since, and is left as the audit stated it rather than revised by
guess — M0 through M2/S2.4 have since replaced a good deal of declaration with
behaviour, but no one has counted again.

---

## Prerequisites

- Python **3.12** · Poetry 1.8+
- Node.js 20+ · npm
- Docker + Docker Compose v2
- An Anthropic API key
- Git

---

## Local Development

> [!WARNING]
> **The full stack is not runnable yet.** The *frontend* half now is: its image
> builds and serves every route (Sprint M0/S0.4), and `poetry check` passes
> (M0/S0.1). What remains unproven is the backend half of `docker compose up`
> — finishing it is the rest of **Milestone M0**.

### What works today

```bash
git clone <repository-url>
cd researchmind-mcp

cp .env.example .env                    # then set ANTHROPIC_API_KEY
cd frontend && cp .env.example .env.local && cd ..

./scripts/check-hygiene.sh              # repository hygiene checks
```

The frontend runs on its own (verified in Sprint M0/S0.4):

```bash
cd frontend
npm ci                                  # reproducible: installs from package-lock.json
npm run dev                             # http://localhost:3000
npm run build                           # production build, emits .next/standalone
npm run lint                            # ESLint via next/core-web-vitals
npm run type-check                      # tsc --noEmit
```

`NEXT_PUBLIC_*` variables are inlined into the browser bundle **at build time**,
so they must be set before `npm run build`, not on the running container. See
[docs/development/environment.md](docs/development/environment.md).

### After Milestone M0 (not yet available)

```bash
docker compose up --build               # full stack — backend half still unproven
poetry install && poetry run python main.py   # backend only
```

This section is updated as each milestone makes a workflow genuinely
functional. Commands are not documented here before they work.

---

## Testing

```bash
poetry run pytest                       # ✅ 749 passed, 2 xfailed
poetry run ruff check .                 # ✅ enforced in CI
poetry run black --check .              # ✅ enforced in CI
poetry run mypy .                       # ⚠️ 121 errors repo-wide; strict-clean and CI-enforced over the M2 security surface
./scripts/check-hygiene.sh              # ✅ enforced in CI
```

The two `xfail`s are deliberate and `strict`: they pin behaviour that does not
exist yet (chunking, M3; readiness probing, M9) and will fail the build the day
it starts working, rather than passing silently.

Strategy, test levels and the blocking release-gate suites are in
[docs/development/testing.md](docs/development/testing.md).

---

## Git Workflow

`main` (protected, validated states only) ← `milestone/*` ← `sprint/*`.
Conventional Commits. Tags mark validated milestones, never aspirational ones.

Full detail: [docs/development/workflow.md](docs/development/workflow.md).

---

## Roadmap

| Milestone | Outcome |
|---|---|
| **M0** | Build integrity — images build, imports resolve, CI green |
| **M1** | System of record — Postgres, repositories, migrations |
| **M2** | Fail-closed authentication and authorisation *(complete — S2.1–S2.5)* |
| **M3** | Document ingestion — PDF to owned, section-aware chunks *(S3.1 upload & storage done)* |
| **M4** | Tenant-isolated retrieval |
| **M5** | **Grounded answering — first working end-to-end flow** |
| **M6** | MCP adapter |
| **M7** | RAG evaluation harness |
| **M8** | Web client |
| **M9** | Hardening and observability |
| **M10** | Release validation |

[docs/roadmap/MILESTONES.md](docs/roadmap/MILESTONES.md) ·
[docs/roadmap/COMPLETION_PLAN.md](docs/roadmap/COMPLETION_PLAN.md)

---

## Security

Authentication and authorisation are **fail-closed** as of M2: accounts are
created and signed into over HTTP, every protected route refuses a token it
cannot resolve to an active user (401), refuses a role that lacks the permission
(403), and scopes every document to its owner in the SQL itself (404). There is
still **no rate limiting** (M9), and the system is pre-alpha, so it must not be
exposed to an untrusted network. Principles
and invariants: [docs/security/principles.md](docs/security/principles.md).
To report a vulnerability, see [SECURITY.md](SECURITY.md) — please report
privately.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
