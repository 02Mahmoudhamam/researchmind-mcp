# Retrieval

M4/S4.1 (COMPLETION_PLAN 3.13, ADR-0003 §5,
[ADR-0014](../adr/0014-retrieval-foundation.md)). How an authenticated user's
question becomes chunks they are allowed to see. The stage before it is
[embeddings.md](embeddings.md); the worker that produces what this reads is
[ingestion.md](ingestion.md).

```text
Principal (authenticated)            backend/services/search_service.py
   │
   ▼ validate            NFC, non-empty, fits the model's input
   ▼ EmbeddingProvider.embed_query   ← shared/interfaces/embedding.py
   │    the same model that embedded the documents
   ▼ VectorStore.search              ← shared/interfaces/vector_store.py
   │    owner_id REQUIRED, applied inside Qdrant's filter
   ▼ candidate ids + scores          ← nothing else from the index is used
   ▼ one owner-scoped SELECT         backend/db/repositories/document_chunk.py
   │    ownership + not deleted + READY + this model
   ▼ RetrievalResult, index order preserved
```

## Qdrant proposes; PostgreSQL disposes

The vector store returns **ids and scores**. Its payload is not read here — not
its `user_id`, not its `document_id`, not for a comparison and not for a check.

That is stronger than comparing payload against row, and simpler, because there
is no comparison to get wrong. Validation looks up chunk ids and takes the
document id, the ownership, the status and the text from the database. A stale
payload, a payload written by an older pipeline and a payload forged by anyone
with write access to Qdrant are therefore all the same thing: a chunk id, which
either resolves to a row this principal owns or does not.

An architecture test asserts `.payload` does not appear in the service at all.

## The owner is the Principal, and nothing else

```python
await service.search(RetrievalQuery(text="how was recall measured?"), principal)
```

`RetrievalQuery` has **no owner field**. Searching as someone else is not a
request that gets rejected; it is a request that cannot be expressed — the same
shape as `Principal.is_active` being `Literal[True]` rather than a `bool` that
someone must remember to check.

The same `principal.user_id` reaches both layers: Qdrant's filter and the SQL
predicate. `principles.md` §3 requires the ownership filter to be a required
parameter that appears in the query, and both do.

## Validation, in one statement

```sql
SELECT ... FROM document_chunks
JOIN documents ON documents.id = document_chunks.document_id
WHERE document_chunks.id = ANY(:chunk_ids)
  AND documents.user_id      = :user_id
  AND documents.deleted_at   IS NULL
  AND documents.status       = 'ready'
  AND document_chunks.embedding_model_id = :model_id
  AND document_chunks.dimension          = :dimension
```

One query for the whole batch — not N queries, and not one unscoped query
filtered in Python, which would mean the rows were read before the check.

| Condition | Why a candidate fails it |
|---|---|
| the chunk exists | its row was deleted after the vector was written |
| the document is this user's | a stale or forged payload proposed someone else's |
| the document is not deleted | soft-deleted; its vectors are still in Qdrant |
| the document is `ready` | re-chunking, re-embedding, or it failed |
| the model matches | embedded by a superseded model, so the score is meaningless |

A candidate that fails any of them is **dropped**, silently and without error.
"Not yours", "not there" and "not searchable" are one answer, as everywhere else
in the repositories.

### Why the model must match

The query vector comes from the active provider. A chunk embedded by a
different model lives in a different vector space, so its similarity to this
query is not a ranking — it is a number. Dropping it fails closed: after a model
change, documents disappear from results until they are re-embedded (which the
S3.5 re-chunk stage does automatically on the next delivery). Silence is the
conservative failure; a plausible score from the wrong space is not.

## Eligibility is `ready`, and only `ready`

| Status | Searchable |
|---|---|
| `pending`, `processing`, `parsed`, `chunked`, `failed` | no |
| `ready` | **yes** |
| soft-deleted (any status) | no |

**The existence of a vector proves nothing.** S3.5 deferred deleting vectors
when a document is deleted (ADR-0003 §6 is still half-implemented), so vectors
outlive the documents they describe by design. The row's status at the moment of
the query is what decides. Tests parametrize every non-`ready` status with the
vectors deliberately left in place.

## Stale vectors

The case ADR-0003 predicted and this design exists to make harmless:

```text
Qdrant      a vector for chunk X          (still there)
PostgreSQL  document deleted, or not ready
Search      returns nothing for X
```

Not an authorization failure, not an infrastructure failure — **fewer results**.
A user whose candidates are mostly stale simply gets fewer than `top_k`, because
top-k is counted before validation. ADR-0003's Consequences call this the
degradation that makes the dual-write failure mode safe: "the system degrades to
*fewer results*, never *leaked another user's manuscript*."

A corrupted index can hide a chunk. It cannot hand one over. There is a test for
exactly that: a chunk re-upserted with a payload claiming another owner
disappears from its real owner's results (the filter reads the payload) and
still does not appear in the impostor's (the database does not).

## Order and duplicates

Results keep **Qdrant's descending-score order**, with dropped candidates
removed and the survivors' relative order untouched. The database returns rows
in whatever order suits it, and that order is discarded.

```text
candidates   A .91   B .87   C .82        B fails validation
results      A .91   C .82
```

Duplicate candidate ids — which a correct Qdrant does not produce, since a point
id is unique — collapse to the **first** occurrence, which carries the highest
score because the store returns them in order. Deduplication happens *before*
validation, so a duplicate does not consume two slots of the batch.

## Query validation

| Rule | |
|---|---|
| normalised | NFC — the form `pdf_parser.py` leaves the corpus in, so query and documents are compared in one normal form |
| stripped | leading and trailing whitespace |
| non-empty | refused after stripping, including ` ` and `　` |
| at most `RETRIEVAL_MAX_QUERY_CHARS` | a cheap bound, checked before tokenizing |
| at most `max_input_tokens - 2` | the real limit: past it the model truncates and the vector describes a question nobody asked |

**The query text is never logged**, and never appears in an error message — it
is a user's research question. Log lines carry counts and the owner id.

## Errors

| Situation | Result |
|---|---|
| no candidates matched | `()` |
| every candidate dropped at validation | `()` |
| empty or over-long query | `InvalidSearchQuery` |
| provider, vector store or database failed | `SearchUnavailable` |

**An unavailable dependency is never an empty result.** A caller that cannot
tell an outage from "no matches" will present the first as the second, and in M5
that means an answer that cites nothing and sounds certain. The two exception
types are separate because the caller's remedy is different: one is "fix your
query", the other is "try again later".

No provider or driver exception escapes. The messages are built in the service
from nothing the failure supplied, so they carry no host, URL, credential, stack
trace or query text.

## Transactions

Read-only, and no transaction is held across a network call:

```text
validate → embed → Qdrant search → short PostgreSQL read → return
```

The read transaction is closed before returning, including when validation
raises. Repositories still do not commit (M1/S1.4).

## Configuration

| Variable | Default | |
|---|---|---|
| `RETRIEVAL_TOP_K` | 10 | how many candidates to ask the index for. Candidates, not results — validation may drop some |
| `RETRIEVAL_SCORE_THRESHOLD` | 0.7 | passed to Qdrant; below it a candidate is not returned at all |
| `RETRIEVAL_MAX_QUERY_CHARS` | 2000 | the cheap bound above |

A `RetrievalQuery` may override the first two per search; both are validated by
the model (`top_k > 0`, `0 ≤ threshold ≤ 1`) and at startup by Settings.

**The threshold is a Qdrant cosine similarity** over L2-normalised vectors from
`bge-small-en-v1.5` (ADR-0013). It is not a probability and it is not comparable
across embedding models; changing the model changes what 0.7 means.

## No HTTP route yet

`POST /api/v1/search` still answers **501**.

Not caution: exposing it means deciding whether the API process loads a 67 MB
embedding model and holds a Qdrant connection pool. The API's `lifespan`
deliberately does nothing — so a momentarily unavailable dependency cannot
crash-loop the process and stop `/health` answering — and ADR-0004/0005/0013 put
the model and the client in the **worker's** composition root. ADR-0014 §9
records that as the next M4 sprint's decision.

Until then `SearchService` is constructed directly, as the integration tests do.

## Not done

- **No route**, as above; no MCP tool either (M6).
- **No generation.** `AgentInput.context` is still populated by nothing (M5).
- **No reranking, no hybrid or BM25 search, no query expansion.**
- **No parent-document retrieval** — ADR-0007 deferred it; `section` and the
  page span are what a citation resolves against for now.
- **Vectors are still not deleted with their documents** (ADR-0003 §6). This
  sprint makes that harmless, not fixed.
