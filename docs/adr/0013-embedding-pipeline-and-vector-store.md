# ADR-0013 — The embedding stage: an authoritative tokenizer, versioned re-chunking, and vectors before READY

- **Status:** Proposed — implemented in Milestone M3/S3.5; supersede if rejected
- **Date:** 2026-09-16
- **Deciders:** Implementation of M3/S3.5, pending owner review
- **Related:** ADR-0003 §4–§6, ADR-0004, ADR-0005, ADR-0007, ADR-0009, ADR-0011, ADR-0012; Milestone M3/S3.5

## Context

M3/S3.4 leaves a document `chunked`: section-aware chunks stored with their
provenance — `strategy_version` and `tokenizer_id`. That tokenizer,
`regex-word/v1`, was **explicitly provisional** (ADR-0012 §2): a stand-in behind
a seam, kept until the embedding model was chosen, and recorded on every chunk
precisely so the chunks it sized could be found again.

The model is now chosen and installed: ADR-0004's FastEmbed
`BAAI/bge-small-en-v1.5`. Measured, not assumed: **384 dimensions**,
deterministic output for the same input, vectors already L2-normalised (so
cosine and dot agree), **512 input tokens before silent truncation**, and its
WordPiece tokenizer is reachable as a `tokenizers.Tokenizer` **with character
offsets** — which is exactly the shape S3.4's span-based `Tokenizer` protocol
requires.

Four things were undecided.

1. **What happens to chunks sized by the provisional tokenizer.** Their token
   counts, and therefore their boundaries, were made by a different ruler.
   Embedding them as they are would bake a stand-in's decisions into the index
   permanently, and ADR-0012 promised the opposite.
2. **What `ready` requires.** ADR-0011 fixed it as "searchable"; ADR-0012 said
   chunks alone are not. Nothing had yet said what is.
3. **How a vector is identified**, so that a retry, a duplicate delivery or a
   crash mid-write cannot leave two vectors for one chunk.
4. **What the vector store's interface looks like.** The scaffold's
   `BaseVectorStore.search(..., filters: Optional[dict] = None)` makes the owner
   filter *optional*, which `docs/security/principles.md` §3 forbids: the
   ownership filter must be "a required parameter of the repository signature",
   and "the safe path must be the only path".

## Decision

### 1. The embedding model's tokenizer is authoritative

`FastEmbedTokenizer`, id **`bge-small-en-v1.5/wordpiece`**, implements the
S3.4 `Tokenizer` protocol over the model's own vocabulary, using the offsets
`tokenizers` reports so chunk content stays a verbatim substring. Special
tokens (`[CLS]`, `[SEP]`, offsets `(0, 0)`) are excluded from counts: they are
per-sequence overhead, not text.

The worker chunks with it. `regex-word/v1` remains in the tree as a
dependency-free implementation used where no model should be loaded, and is no
longer what sizes anything that will be embedded.

**A chunk may not exceed what the model reads.** `CHUNK_SIZE_TOKENS` plus the
two special tokens must fit the provider's `max_input_tokens`; Settings and the
worker refuse otherwise, because the failure it prevents is silent — the model
truncates and the tail of a chunk is embedded as though it were not there.

### 2. Re-chunking is version-driven, not a migration

Before embedding, the stage compares each chunk's `strategy_version` and
`tokenizer_id` with the pipeline's current pair. On any mismatch the document is
**re-chunked from its stored pages** — the same deterministic chunker S3.4
built — its previous chunks and their vectors deleted, and the new chunks
written, all before a vector is produced.

This is a rule, not a one-off: any future strategy or tokenizer change is
detected the same way, by data rather than by a dated script. Provenance is not
destroyed — every chunk still records what made it, and the document's pages,
which are the source, are untouched.

**Vectors of different versions are never mixed.** A document's vectors are
deleted before its new chunks are upserted, so the collection never holds two
generations of the same document.

### 3. `ready` means the vectors are in the store

```
chunked → processing → (re-chunk if required) → embed → upsert → ready
```

`ready` is set **after** the vector store has accepted the upsert, in a
transaction that also records `embedding_model_id` and `dimension` on every
chunk. Nothing else may set it. A document that reaches `ready` therefore has
chunks, embeddings of the current model, and vectors — the three things
"searchable" needs.

**Write order is deliberate: Qdrant first, then the database.** The two stores
cannot share a transaction (ADR-0008 made the same point about the filesystem).
A crash between them leaves vectors for a document that is still `processing` —
reachable by the reaper, and harmless, because the upsert is idempotent and the
retry overwrites them. The reverse order would allow `ready` with no vectors,
which is the one state a client cannot detect.

### 4. A vector's id is its chunk's id, and a chunk's id is derived

ADR-0003 already says a chunk's id *is* its Qdrant point id. S3.5 makes that id
**deterministic**: `uuid5` over the document id, chunk index, strategy version
and tokenizer id. Re-running the chunker with the same configuration produces
the same ids; upserting the same chunk twice overwrites one point rather than
creating two. Changing the strategy or tokenizer changes the ids, which is why
the old vectors are deleted rather than left to collide.

### 5. The owner filter is a parameter, not an option

`VectorStore.search(query_vector, *, owner_id, ...)` — a `Protocol` replacing
the scaffold's ABC. The owner is required at the signature, applied inside the
Qdrant `Filter`, and cannot be omitted by a caller who forgets a `filters`
dict. ADR-0003 §5's second layer — re-validating returned ids against
PostgreSQL — remains M4's to implement when retrieval reaches an API.

Payloads carry what ADR-0003 §4 asks and what a citation needs: `user_id`,
`document_id`, `chunk_id`, `chunk_index`, `section`, `page_start`, `page_end`,
`strategy_version`, `tokenizer_id`, `embedding_model_id`, `dimension`. Keyword
payload indexes on `user_id` and `document_id`. **No chunk text, no
credentials, no `Principal`.**

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Embed the existing chunks as they are | Skip re-chunking | Makes a stand-in's boundaries permanent and breaks ADR-0012's promise that the tokenizer was replaceable. |
| A one-off migration to re-chunk today's documents | A dated script | Works once, for documents that exist today; the next model change needs another one. The version columns already say which chunks are stale. |
| Re-chunk everything unconditionally | Simpler rule | Throws away and rebuilds chunks that are already correct, and re-embeds them at cost, every run. |
| `ready` after embedding, before upsert | Fewer moving parts | A document could be `ready` with nothing in the index — the failure a client cannot see. |
| Database first, then Qdrant | Conventional order | Same fault: `ready` committed, vectors missing. |
| Two-phase commit across stores | Strict atomicity | Qdrant offers no such protocol; the honest answer is an idempotent write and a status that lags it. |
| `uuid4` chunk ids (S3.4's) | No change | A retry after a partial write would create a second vector for the same text. |
| Hash the chunk *content* for the id | Content-addressed points | Two identical chunks in one document would collide, and a re-chunk that changed nothing would still churn ids on any whitespace difference. |
| Keep `BaseVectorStore` as it is | No interface change | Its optional `filters` makes the unsafe call the easy one; principles.md §3 forbids exactly that. |
| Delete vectors on document delete, here | Close ADR-0003 §6 | The API's delete path would gain a vector-store dependency this sprint did not design or test. Recorded as deferred, with the ADR-0003 §5 re-validation as the standing mitigation. |

## Consequences

**Good.** The tokenizer that sizes a chunk is the one that embeds it. A model or
strategy change is detected from data and repaired by re-running a
deterministic stage. A document is `ready` only when it is genuinely
searchable. Idempotent point ids make retries, duplicate deliveries and crashes
converge on one correct state. Ownership is in the query, not in a caller's
discipline.

**Bad.** Every document chunked before S3.5 is re-chunked once, and the work is
paid on first delivery after deployment. The model is a 67 MB asset that must
be in the image, and CI must have it. A crash between the two stores can leave
vectors for a `processing` — later `failed` — document until a retry or the
cleanup path removes them. Deleting a document still leaves its vectors behind
(ADR-0003 §6 remains half-implemented), mitigated only by §5's re-validation,
which M4 owns.

**Neutral.** `regex-word/v1` stays in the tree. `document_chunks` needs no new
column: `embedding_model_id` and `dimension` were added for exactly this, and
are now written. The vector store holds one generation of one document's
vectors at a time.

## References

- ADR-0003 §4–§6 — payload denormalisation, the isolation invariant, deletion order
- ADR-0004 — FastEmbed `bge-small-en-v1.5`, weights baked into the image
- ADR-0005 — provider-derived dimension, `model_id`, startup assertion
- ADR-0011 §1, ADR-0012 §1–§2 — `ready` means searchable; the tokenizer seam this ADR redeems
- `docs/security/principles.md` §3 — the ownership filter as a required parameter
- `docs/development/embeddings.md` — the implementation
