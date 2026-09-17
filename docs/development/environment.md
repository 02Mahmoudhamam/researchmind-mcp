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

pydantic-settings defaults to `extra="forbid"`. The original single
`.env.example` contained two frontend-only variables (`NEXT_PUBLIC_API_URL`,
`NEXT_PUBLIC_APP_NAME`) that `Settings` does not declare, so the documented
Quick Start —

```bash
cp .env.example .env
```

— caused `Settings()` to raise `ValidationError: extra_forbidden` before the
application could start.

That was fixed in two steps. Splitting the files by consumer removed the
immediate cause. Sprint M0/S0.1 then set `extra="ignore"` on `Settings`, so an
undeclared key in a local `.env` is skipped instead of crashing the process.
Verified: a `.env` containing `DATABASE_URL` (a key Milestone M1 introduces)
previously raised `ValidationError` and now loads cleanly, while required
fields such as `ANTHROPIC_API_KEY` are still enforced.

Keeping the files split still matters. `extra="ignore"` stops an unknown key
from being fatal; it does not make frontend configuration belong in the backend
environment file.

### The rule

**A variable is added to `.env.example` in the same commit as the `Settings`
field that consumes it.** Never earlier. An unknown key is now tolerated rather
than fatal, but an example file that advertises variables the backend does not
read is misleading.

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

> **Since Sprint M2/S2.1 the application refuses to start** outside
> `APP_ENV=development` if either secret is a known placeholder (`changeme`,
> `secret`, `your-secret-key-here`, …), empty, or shorter than 32 characters.
> The error names the setting and the problem and never echoes the value — an
> error that prints a secret puts it in logs and issue reports.
>
> 32 characters is not arbitrary: PyJWT raises `InsecureKeyLengthWarning` below
> it for HS256 (RFC 7518 §3.2).

### Optional — application

| Variable | Default |
|---|---|
| `APP_NAME` | `ResearchMind MCP` |
| `APP_ENV` | `development` |
| `APP_PORT` | `8000` |
| `DEBUG` | `false` |
| `LOG_LEVEL` | `INFO` |
| `LOG_FORMAT` | `json` |

### Optional — document storage and upload limits (M3/S3.1)

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_ROOT` | `uploads` | Relative to the working directory — `/app/uploads` in the image, which docker-compose.yml mounts. Uploads land at `{STORAGE_ROOT}/{user_id}/{sha256}.pdf` ([ADR-0008](../adr/0008-local-content-addressed-object-storage.md)) |
| `MAX_UPLOAD_BYTES` | `52428800` (50 MiB) | Over it: 413. Must be positive |
| `MAX_PDF_PAGES` | `500` | Over it: 422. Must be positive |

Tests set `STORAGE_ROOT` to a per-run temporary directory in `tests/conftest.py`.

### Optional — ingestion worker (M3/S3.2)

| Variable | Default | Notes |
|---|---|---|
| `ARQ_MAX_JOBS` | `10` | Jobs one worker process runs concurrently. Must be positive |
| `INGEST_JOB_TIMEOUT_SECONDS` | `300` | ARQ cancels a job that runs longer. Must be positive |
| `INGEST_MAX_TRIES` | `5` | Attempts for a transient failure, the first included; the last records `failed`. Must be positive |
| `INGEST_STALE_PROCESSING_SECONDS` | `900` | The reaper fails a document `processing` for longer. **Must be greater than `INGEST_JOB_TIMEOUT_SECONDS`** — `Settings` refuses to start otherwise, because a threshold at or below the timeout could fail a job that is still running |
| `INGEST_PARSE_TIMEOUT_SECONDS` | `180` | *(M3/S3.3)* Budget for extracting one PDF's text; past it the parsing process is killed and the document fails as `pdf_timeout`. Must be positive and **less than `INGEST_JOB_TIMEOUT_SECONDS`** — otherwise ARQ would cancel the job before the document could record why. See [pdf-extraction.md](pdf-extraction.md#time) |

The page cap the worker enforces is `MAX_PDF_PAGES`, the same setting upload
uses — not a second one.

### Optional — chunking (M3/S3.4)

| Variable | Default | Notes |
|---|---|---|
| `CHUNK_SIZE_TOKENS` | `400` | Tokens per chunk — ADR-005's "~400 tokens". Since M3/S3.5 counted by the **embedding model's own tokenizer**; the worker refuses to start unless this plus 2 special tokens fits the model's 512-token input |
| `CHUNK_OVERLAP_TOKENS` | `60` | Tokens shared with the previous chunk — ADR-005's "~15% overlap". Must be ≥ 0 and **less than `CHUNK_SIZE_TOKENS`**, or the windows would not advance |

See [chunking.md](chunking.md). Reference chunks ignore the overlap by design.

### Optional — embeddings and retrieval (M3/S3.5)

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDING_PROVIDER` | `fastembed` | The only implemented provider (ADR-0004). Anything else is refused at startup rather than silently substituted |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | 384 dimensions, 512 input tokens. Must not be blank |
| `EMBEDDING_BATCH_SIZE` | `32` | Texts per call into the model. Must be positive |
| `EMBEDDING_CACHE_DIR` | unset | Where the weights live. The image sets `/app/.fastembed_cache` and bakes them in, so no container downloads them |
| `RETRIEVAL_TOP_K` | `10` | Validated here; used by retrieval in M4 |
| `RETRIEVAL_SCORE_THRESHOLD` | `0.7` | Must be between 0 and 1 |
| `QDRANT_TIMEOUT_SECONDS` | `30` | Must be positive |

**There is no dimension variable, deliberately.** ADR-0005 makes it a property
of the active provider: the collection is created from `provider.dimension` and
a literal cannot drift from the model. See [embeddings.md](embeddings.md).

The API and the worker read the same variables — including `STORAGE_ROOT`,
`DATABASE_URL` and `REDIS_*`, which must agree between them. Under
`docker compose` both services set `REDIS_HOST=redis`. See
[ingestion.md](ingestion.md).

### Optional — services

| Variable | Default |
|---|---|
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_COLLECTION` | `localhost` / `6333` / `researchmind` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_TTL` | `localhost` / `6379` / `0` / `86400` |

Redis carries the ingestion queue since M3/S3.2. Tests use `REDIS_DB=15`
(`tests/conftest.py`) and flush it, so a developer's database 0 is never
touched. Qdrant holds the vectors since M3/S3.5; tests set
`QDRANT_COLLECTION=researchmind_test` and each one builds and drops a
uniquely-named collection inside it, so `researchmind` is never touched.

### Optional — PostgreSQL *(added in M1/S1.1)*

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://researchmind:researchmind@localhost:5432/researchmind` | **Must use the `postgresql+asyncpg://` scheme.** A `field_validator` on `Settings` rejects a synchronous DSN, because `postgresql://` resolves to psycopg2 — neither installed nor async — and the resulting error names neither this setting nor the fix |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | Concurrent sessions cap at the sum of the two |
| `DB_POOL_TIMEOUT` | `30` | Seconds to wait for a free pool slot |
| `DB_ECHO` | `false` | Echoes every statement **including values**. Local debugging only |

The default host is `localhost`, matching `QDRANT_HOST` and `REDIS_HOST`: these are
infrastructure endpoints for a developer running the API on the host, not secrets.
The credentials are the development ones declared in `docker-compose.yml`.

> **Under `docker compose` the backend service overrides `DATABASE_URL`** with host
> `postgres`, which resolves only inside the compose network. `NEXT_PUBLIC_*` variables
> are baked in at build time; `DATABASE_URL` is read at runtime, so changing it needs no
> rebuild.

### Optional — auth

| Variable | Default | Notes |
|---|---|---|
| `JWT_ALGORITHM` | `HS256` | Pinned explicitly at decode; never read from the token header. Constrained to `HS256`/`HS384`/`HS512`, so `none` is refused at startup |
| `JWT_EXPIRE_MINUTES` | `60` | Access tokens only. No refresh token and no server-side revocation, so a stolen token is valid until expiry *(M2/S2.1)* |

### Optional — CORS *(added in M2/S2.1)*

| Variable | Default | Notes |
|---|---|---|
| `CORS_ORIGINS` | `http://localhost:3000` | **Comma-separated**, not JSON. Matches the frontend dev server and its published compose port. Replaces the previous `allow_origins=["*"]` |

Credentials are **not** enabled: the frontend sends an `Authorization` bearer
header rather than a cookie (`frontend/src/lib/api.ts`), so credentialed CORS
buys nothing — and it is the setting that would make a permissive origin policy
genuinely dangerous.

### Development-only

| Variable | Notes |
|---|---|
| `DEBUG=true` | Enables `/docs`. **Must be `false` in production** — the OpenAPI UI is disabled when `DEBUG` is off. |

## Frontend variables

| Variable | Default | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | **Public** — visible in the browser bundle. Read by `frontend/src/lib/api.ts`, which no page imports yet |
| `NEXT_PUBLIC_APP_NAME` | `ResearchMind` | **Public**. No source file reads it yet *(verified M0/S0.4)* |

> These are **build-time** values. `next build` inlines every `NEXT_PUBLIC_*`
> variable into the bundle, so setting one on a running container cannot change
> it. `docker-compose.yml` therefore passes `NEXT_PUBLIC_API_URL` as a **build
> arg**; it was previously under `environment:`, where it could never take
> effect *(corrected in M0/S0.4)*.
>
> The value is consumed by the browser on the host, so it uses the published
> port — the compose-internal hostname `backend` does not resolve there.

## Planned variables

Documented so the configuration surface is predictable. **Not yet present in
`.env.example`** — each arrives with the `Settings` field that reads it.

| Variable | Milestone | Purpose | ADR |
|---|---|---|---|
| `CLAUDE_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | M5 | Generation | [0006](../adr/0006-single-research-agent.md) |
| `RATE_LIMIT_*` | M9 | Rate limiting | — |

> `MCP_SERVER_HOST` and `MCP_SERVER_PORT` **were removed in Sprint M0/S0.2**.
> The MCP server uses stdio transport and never binds a port; dead
> configuration that contradicts the code is worse than absent configuration.
> See [ADR-0002](../adr/0002-rest-and-mcp-as-sibling-adapters.md).

## Secret handling

- Real secrets never enter Git. `.gitignore` excludes `.env` and `.env.*` while
  allowing the two example files.
- Real secrets never enter an image. `.dockerignore` excludes `.env*`; without
  it, `COPY . .` in every Dockerfile would bake a developer's key into a layer.
- Example files contain **placeholders only** — never a real value, not even an
  expired one.
- If a key is ever committed, **rotate it first**, then clean history. Removing
  it from Git does not un-disclose it.
