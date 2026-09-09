# Environment Configuration

## Strategy

Configuration is split by consumer, because the backend and the frontend have
different runtimes and different trust boundaries:

| File | Consumer | Committed |
|---|---|---|
| `.env.example` | Backend + worker + MCP server | ✅ placeholders only |
| `.env` | Local backend | ❌ never |
| `frontend/.env.example` | Next.js build and browser | ✅ placeholders only |
| `frontend/.env.local` | Local frontend | ❌ never |

### Why the split

`Settings` uses pydantic-settings, which defaults to `extra="forbid"`. The
original single `.env.example` contained two frontend-only variables
(`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_NAME`) that `Settings` does not
declare, so the documented Quick Start —

```bash
cp .env.example .env
```

— caused `Settings()` to raise `ValidationError: extra_forbidden` before the
application could start. Splitting the files fixes the first command a new
contributor runs.

### The rule

**A variable is added to `.env.example` in the same commit as the `Settings`
field that consumes it.** Never earlier. Adding a variable the backend does not
declare re-creates the failure above.

Anything prefixed `NEXT_PUBLIC_` is **embedded in the browser bundle and is
public**. Never put a secret behind that prefix.

## Backend variables — current

These are the variables `Settings` declares today. Additional variables arrive
with the milestones that introduce them (below).

### Required

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key. **No default — the application will not start without it.** |

### Required in production, defaulted in development

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | `changeme` | ⚠️ Application-level secret |
| `JWT_SECRET` | `changeme` | ⚠️ JWT signing key |

> **From Sprint M2/S2.1 the application refuses to start** if either is still
> `changeme` and `APP_ENV != development`.

### Optional — application

| Variable | Default |
|---|---|
| `APP_NAME` | `ResearchMind MCP` |
| `APP_ENV` | `development` |
| `APP_PORT` | `8000` |
| `DEBUG` | `false` |
| `LOG_LEVEL` | `INFO` |
| `LOG_FORMAT` | `json` |

### Optional — services

| Variable | Default |
|---|---|
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_COLLECTION` | `localhost` / `6333` / `researchmind` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_TTL` | `localhost` / `6379` / `0` / `86400` |

### Optional — auth

| Variable | Default | Notes |
|---|---|---|
| `JWT_ALGORITHM` | `HS256` | Pinned explicitly at decode; never read from the token header |
| `JWT_EXPIRE_MINUTES` | `1440` | Reduced to `60` at M2 — access tokens only, no refresh |

### Development-only

| Variable | Notes |
|---|---|
| `DEBUG=true` | Enables `/docs`. **Must be `false` in production** — the OpenAPI UI is disabled when `DEBUG` is off. |

## Frontend variables

| Variable | Default | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | **Public** — visible in the browser bundle |
| `NEXT_PUBLIC_APP_NAME` | `ResearchMind` | **Public** |

## Planned variables

Documented so the configuration surface is predictable. **Not yet present in
`.env.example`** — each arrives with the `Settings` field that reads it.

| Variable | Milestone | Purpose | ADR |
|---|---|---|---|
| `DATABASE_URL` | M1 | Postgres async DSN | [0003](../adr/0003-postgresql-system-of-record.md) |
| `STORAGE_ROOT` | M3 | Object storage root | [0008](../adr/0008-local-content-addressed-object-storage.md) |
| `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES` | M3 | Upload validation limits | — |
| `CHUNK_SIZE_TOKENS`, `CHUNK_OVERLAP_TOKENS` | M3 | Chunking, in tokens | [0007](../adr/0007-defer-parent-document-retrieval.md) |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_BATCH_SIZE` | M4 | Embedding provider | [0004](../adr/0004-local-fastembed-embeddings.md) |
| `RETRIEVAL_TOP_K`, `RETRIEVAL_SCORE_THRESHOLD` | M4 | Retrieval tuning | [0005](../adr/0005-provider-derived-embedding-dimension.md) |
| `CLAUDE_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | M5 | Generation | [0006](../adr/0006-single-research-agent.md) |
| `ARQ_MAX_JOBS`, `INGEST_JOB_TIMEOUT_SECONDS` | M3 | Job queue | [0009](../adr/0009-arq-for-asynchronous-ingestion.md) |
| `CORS_ALLOWED_ORIGINS` | M2 | Replaces the wildcard | — |
| `RATE_LIMIT_*` | M9 | Rate limiting | — |

> `MCP_SERVER_HOST` and `MCP_SERVER_PORT` are **removed** at M0. The MCP server
> uses stdio transport and never binds a port; dead configuration that
> contradicts the code is worse than absent configuration. See
> [ADR-0002](../adr/0002-rest-and-mcp-as-sibling-adapters.md).

## Secret handling

- Real secrets never enter Git. `.gitignore` excludes `.env` and `.env.*` while
  allowing the two example files.
- Real secrets never enter an image. `.dockerignore` excludes `.env*`; without
  it, `COPY . .` in every Dockerfile would bake a developer's key into a layer.
- Example files contain **placeholders only** — never a real value, not even an
  expired one.
- If a key is ever committed, **rotate it first**, then clean history. Removing
  it from Git does not un-disclose it.
