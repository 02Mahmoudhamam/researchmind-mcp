# Embeddings, the vector store, and READY

M3/S3.5 (COMPLETION_PLAN 3.8–3.9, ADR-0004, ADR-0005,
[ADR-0013](../adr/0013-embedding-pipeline-and-vector-store.md)). How a
`chunked` document becomes **`ready`** — which means searchable, and nothing
else in the system sets it. The stage before it is
[chunking.md](chunking.md); the worker around all three is
[ingestion.md](ingestion.md).

```text
document_chunks (owner-scoped)
   │
   ▼ provenance check                backend/services/ingestion_service.py
   │    strategy_version + tokenizer_id vs the pipeline's
   │    mismatch ─► delete vectors ─► re-chunk from stored pages ─► commit
   ▼ EmbeddingProvider.embed_documents   ← shared/interfaces/embedding.py
   │    FastEmbed bge-small-en-v1.5, 384 dimensions, no transaction held
   ▼ VectorStore.upsert                  ← shared/interfaces/vector_store.py
   │    Qdrant, point id == chunk id, wait=True
   ▼ one transaction: documents.status processing → ready
   │                  UPDATE document_chunks SET embedding_model_id, dimension
   ▼ commit
```

## The model

ADR-0004's choice, run locally: **`BAAI/bge-small-en-v1.5`** through FastEmbed.
Measured, not assumed — the numbers below come from the model in this
repository's lockfile:

| | |
|---|---|
| Dimensions | **384** (the scaffold's hard-coded 1536 was OpenAI's) |
| Input limit | **512 tokens**, after which it truncates *silently* |
| Determinism | the same text always gives the same vector |
| Normalisation | L2, so cosine and dot agree |
| Size | ~67 MB, baked into the image |

`FastEmbedProvider` loads it **once, at worker startup** — never inside a job,
because a cold load costs seconds and a cold cache costs a download.
`Dockerfile.backend` runs `scripts/fetch-embedding-model.py` at build time, so
the weights are in the image; that is verified by loading them in the built
image with `--network none`.

**The dimension is measured, not declared.** The provider embeds one probe
string at startup and takes the width of what comes back, then cross-checks it
against FastEmbed's catalogue. A quantized or swapped variant that disagrees
with its own entry is refused there rather than filling a collection with
vectors of the wrong width.

## The model's tokenizer is authoritative

ADR-0012 §2 called `regex-word/v1` provisional and recorded `tokenizer_id` on
every chunk precisely so the day it was replaced could be found by a query.
This is that day. `FastEmbedTokenizer`, id **`bge-small-en-v1.5/wordpiece`**,
implements the same span-based `Tokenizer` protocol over the model's own
WordPiece vocabulary.

Two details that are not decoration:

- **Truncation is switched off** on the tokenizer the chunker uses. It is a
  *copy* of the model's, because the model's own instance truncates at 512 —
  right for embedding, wrong for counting. Counting a 1,600-token section with
  it would report 512, and the chunker would cut one enormous chunk instead of
  many correct ones. The model's instance is left exactly as FastEmbed set it.
- **Special tokens are not counted.** `[CLS]` and `[SEP]` carry no text and are
  per-sequence overhead. They are excluded from chunk sizing and *added back*
  to the budget check below.

`regex-word/v1` stays in the tree: it is dependency-free and used where no
model should be loaded.

### The budget check

`CHUNK_SIZE_TOKENS + 2 ≤ provider.max_input_tokens`, asserted at worker
startup by `refuse_chunks_the_model_cannot_read`. Over that limit the model
truncates and the tail of every long chunk is embedded as though it were not
there — a valid-looking vector for text the model never read, which nothing
downstream can detect. The worker refuses to start instead. With the shipped
400 against 512 there is room to spare.

## Re-chunking is version-driven

Before embedding, the stage compares each chunk's `strategy_version` and
`tokenizer_id` with what this pipeline produces.

| What it finds | What happens |
|---|---|
| One pair, and it matches | embedded as they are |
| One pair, and it differs | re-chunked from the stored pages |
| More than one pair | re-chunked — an interrupted re-chunk left two generations |
| No chunks at all | `no_chunks_to_embed`, permanent |

A re-chunk **deletes the old vectors first**, outside the database
transaction. Their ids derive from the old tokenizer, so the new upsert cannot
overwrite them; left behind they would stay in the collection, searchable and
attributed to this document, from a generation that no longer exists. Then one
transaction replaces the chunks and updates `chunk_count`.

This is a rule rather than a script: the next model change is detected the same
way, by data. The document's **pages are never touched** — they are the source,
and re-chunking derives from them exactly as the first chunking did.

## A chunk's id is derived, and it is the point id

```
uuid5(namespace, "{document_id}|{chunk_index}|{strategy_version}|{tokenizer_id}")
```

ADR-0003 already made a chunk's id its Qdrant point id; S3.5 makes that id
deterministic. Re-running the stage writes the *same* points and overwrites
them, which is what makes a redelivery, a restart, or a crash mid-upsert
converge on one correct state instead of accumulating a second vector for every
chunk. Changing the strategy or tokenizer changes the ids — which is why the
old vectors are deleted rather than left to linger.

Deliberately **not** derived from the content: two identical chunks in one
document would collide, and a re-chunk that changed nothing but whitespace
would churn every id after it.

## The vector store

`VectorStore` is a `Protocol`, and the scaffold's ABC is gone. Its
`search(..., filters: Optional[dict] = None)` made the ownership filter
*optional*, so the unsafe call was the short one — exactly what
`docs/security/principles.md` §3 forbids.

| Operation | Owner |
|---|---|
| `ensure_collection(dimension)` | — (startup only) |
| `upsert(records)` | in every payload |
| `search(vector, *, owner_id, limit, score_threshold, document_ids=None)` | **required** |
| `delete_document(*, document_id, owner_id)` | **required** |
| `count_for_document(*, document_id, owner_id)` | **required** |

`owner_id` is keyword-required and goes **inside the `Filter` the server
evaluates** — `must`, never `should`, so it cannot be satisfied by some other
condition matching instead. `document_ids` joins the same `must`, so it can only
narrow what the owner condition already allows. Nothing is fetched and then
filtered in Python: post-filtering means the wrong rows were already read.

A returned point whose `user_id` is not the searcher's raises rather than being
dropped — defence in depth, and a refusal rather than an empty result, so a
broken query cannot look like "no matches".

**Only `vector_db/qdrant/` imports `qdrant_client`.** An architecture test
asserts it, along with the absence of the literals 384 and 1536 anywhere in the
tree.

### The collection

Created from `provider.dimension`, distance Cosine, with keyword payload
indexes on `user_id` and `document_id` — every query filters on the first, so
without an index the isolation invariant would be a scan. A collection that
already exists at another width is **refused**, not reused: its vectors are in
a different space.

### The payload

`user_id`, `document_id`, `chunk_id`, `chunk_index`, `section`, `page_start`,
`page_end`, `strategy_version`, `tokenizer_id`, `embedding_model_id`,
`dimension`.

A typed record, not a free dictionary: there is **no field** for chunk text, a
credential or a `Principal`, so leaking one is not something a caller can do by
accident. The content stays in PostgreSQL.

## Why Qdrant is written before PostgreSQL

The two stores cannot share a transaction (ADR-0008 made the same point about
the filesystem), so one of them is committed second and a crash between them is
possible. The order is chosen by which failure is detectable.

| Crash point | What is left | Why it is survivable |
|---|---|---|
| Before the upsert | `processing`, no vectors | the retry embeds and upserts |
| Mid-upsert | `processing`, some vectors | the ids are derived; the retry overwrites |
| After the upsert, before the commit | `processing`, all vectors | the retry overwrites and commits |
| After the commit | `ready`, all vectors | done |

The reverse order would allow `ready` with no vectors — the one state a client
cannot detect, because it polls until `ready` and then searches nothing.

Embedding itself is done **holding no transaction**. It is CPU work measured in
seconds; a transaction across it would hold a row lock and a connection at the
mercy of a model.

## Status and the worker

`chunked → processing → ready`. `chunked` stopped being terminal in S3.5: it is
where the embed stage starts, and it joined `pending` and `parsed` in
`RECOVERABLE_STATUSES`, so a document stranded there is re-queued by the sweep.

| Redelivered document | What happens |
|---|---|
| `pending` | parsed, chunked, embedded |
| `parsed` | chunked, embedded |
| `chunked` | embedded (re-chunked first if stale) |
| `processing` | skipped — another delivery holds the claim |
| `ready`, `failed` | skipped as terminal |
| deleted | rejected; the owner-scoped read finds nothing |

## Failures

| Reason | When | Retried |
|---|---|---|
| `no_chunks_to_embed` | a `chunked` document whose chunks are gone, or a stale one whose pages are gone | never |
| `no_chunks_produced` | a re-chunk that produced nothing | never |
| `embedding_dimension_mismatch` | a vector whose width is not the model's | never — the same model against the same collection fails the same way |
| `embedding_failure` | the provider raised | yes, within `INGEST_MAX_TRIES` |
| `vector_store_failure` | the store refused, or could not be reached | yes |
| `chunking_failure` | the re-chunk itself failed | yes |

A transient failure releases the claim back to **`chunked`**, so a retry embeds
and never re-parses. No provider or Qdrant exception is ever re-raised: their
messages carry hosts, URLs and response bodies, and these messages are written
to `failure_reason` and the worker's logs. Logs carry counts, the model id and
the dimension — never chunk text.

## Configuration

| Variable | Default | |
|---|---|---|
| `EMBEDDING_PROVIDER` | `fastembed` | the only implemented one; anything else is refused at startup |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | must not be blank |
| `EMBEDDING_BATCH_SIZE` | 32 | texts per call into the model |
| `EMBEDDING_CACHE_DIR` | unset | where the weights live; the image sets `/app/.fastembed_cache` |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / 6333 | |
| `QDRANT_COLLECTION` | `researchmind` | |
| `QDRANT_TIMEOUT_SECONDS` | 30 | |
| `RETRIEVAL_TOP_K` | 10 | M4 uses it; validated here |
| `RETRIEVAL_SCORE_THRESHOLD` | 0.7 | must be between 0 and 1 |

**There is deliberately no dimension setting.** It is a property of the active
provider (ADR-0005 §2), and a literal that can drift from the model is the bug
this replaces.

## Tests

| Marker | What it needs | What runs |
|---|---|---|
| `embeddings` | the model's weights | the provider and tokenizer, against the real model |
| `qdrant` | a reachable Qdrant | the store and the embed stage, each in its own collection |

Both fail rather than skip in CI (`REQUIRE_EMBEDDINGS=1`, `REQUIRE_QDRANT=1`),
because a skipped test reports success. Locally they skip, and
`poetry run python scripts/fetch-embedding-model.py` or
`docker compose up -d qdrant` turns them back on.

Nothing about ownership is asserted against a double. A mocked store would only
prove the mock filters; the claim is that **the server** does, so the tests ask
a real Qdrant for another tenant's vectors and get nothing back.

## Not done

- **No retrieval API.** `SearchService` is still a stub, and ADR-0003 §5's
  second layer — re-validating returned chunk ids against PostgreSQL — is M4's.
- **No vector deletion on document delete.** ADR-0003 §6 remains
  half-implemented: `delete_document` exists on the store, and the API's delete
  path does not call it yet. Recorded as deferred in ADR-0013.
- **No reranking, no hybrid search, no query expansion.**
- **One collection for every tenant**, separated by the payload filter, which is
  what ADR-0003 chose.
