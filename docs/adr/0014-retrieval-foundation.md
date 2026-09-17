# ADR-0014 — Retrieval: Qdrant proposes, PostgreSQL disposes

- **Status:** Proposed — implemented in Milestone M4/S4.1; supersede if rejected
- **Date:** 2026-09-16
- **Deciders:** Implementation of M4/S4.1, pending owner review
- **Related:** ADR-0003 §4–§6, ADR-0004, ADR-0005, ADR-0007, ADR-0013; `docs/security/principles.md` §3; Milestone M4/S4.1

## Context

M3/S3.5 leaves a document `ready`: chunked, embedded, and its vectors in Qdrant
with `user_id` and `document_id` in the payload and keyword indexes on both.
Nothing reads them. `SearchService` is a stub, and
`backend/api/routers/search.py` answers 501.

ADR-0003 §5 and `principles.md` §3 already fix the shape of retrieval — a fast
path filtered in Qdrant and a correct path validated in PostgreSQL — in
identical words, and `principles.md` is labelled non-negotiable. So the
architecture is not in question. What is undecided is everything the sentence
"re-validated against PostgreSQL ownership" leaves open.

1. **What is re-validated, and against what.** ADR-0003 says ownership. It does
   not say whether a deleted document, a document that is not `ready`, or a
   chunk embedded by a superseded model should also be dropped.
2. **Whether S4.1 exposes an HTTP route.** The route exists and answers 501.
3. **What happens when infrastructure fails.** "Fewer results" is the documented
   degradation for *stale* vectors; it must not become the behaviour for an
   unreachable Qdrant, which is a different event with the same shape.
4. **How results are ordered and deduplicated** once some candidates are dropped.

## Decision

### 1. Qdrant proposes candidate ids; PostgreSQL decides everything else

The vector store returns **ids and scores**. Nothing else it returns is used to
make a decision. In particular the payload's `user_id` and `document_id` are
**never** read for authorization — not compared, not trusted, not consulted.

Validation looks up **chunk ids only**, and takes the document id, the
ownership, the status and the text from PostgreSQL. A stale payload, a payload
written by an older pipeline, and a payload forged by anyone with write access
to Qdrant are therefore all the same thing: a chunk id, which either resolves to
a row this principal owns or does not.

This is stronger than comparing the payload against the database, and simpler.
There is no comparison to get wrong.

### 2. One query, and the predicate is in the SQL

All candidate ids are validated together, in a single owner-scoped statement:

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

Not N queries, and not one unscoped query filtered in Python. `principles.md` §3
requires the ownership filter to be a required parameter that reaches the SQL
predicate; this is that rule applied to a batch.

### 3. Eligibility is `ready`, read from PostgreSQL now

`ready` is the only searchable status (ADR-0011, ADR-0013 §3: it *means*
searchable). `pending`, `processing`, `parsed`, `chunked` and `failed` are all
rejected, and so is a soft-deleted document.

The existence of a vector is **not** evidence of eligibility. S3.5 deliberately
deferred deleting vectors when a document is deleted (ADR-0013, ADR-0003 §6
remains half-implemented), so vectors outlive the documents they describe by
design. The status in the row is what counts, at the moment of the query.

### 4. A chunk embedded by another model is not a candidate

Chunks record `embedding_model_id` and `dimension` (S3.5). The query embedding
is produced by the active provider, so a chunk embedded by a different model is
in a different vector space and its score against this query means nothing. Such
chunks are dropped by the same statement, rather than ranked.

This fails closed: if the pipeline's model changes and documents have not been
re-embedded, they disappear from results until they are. Silence is the
conservative failure; a plausible-looking score from the wrong space is not.

### 5. Ordering is Qdrant's; dropping does not reorder

Results come back in Qdrant's descending-score order, with dropped candidates
removed and the survivors' relative order untouched. The database returns rows
in whatever order it likes, and that order is discarded.

Duplicate candidate ids — which Qdrant should not produce, since a point id is
unique — are collapsed to the **first** occurrence, keeping the highest score.
Deduplication happens before validation, so a duplicate does not consume two
slots of the batch.

### 6. An unavailable dependency is an error, never an empty result

Stale vectors degrade to fewer results. An unreachable Qdrant, a failed
embedding, or an unreachable PostgreSQL raises `SearchUnavailable`. The two
must not look alike: "no matches" is an answer, and "the index is down" is not,
and a caller that cannot tell them apart will present the second as the first.

No provider or driver exception escapes the service, and no message it raises
carries a host, a URL, a credential, a stack trace or the query text.

### 7. Chunk text comes from PostgreSQL, and stays out of Qdrant

ADR-0013 §5 kept text out of the payload. That decision is preserved and is now
load-bearing: the text a caller receives is fetched *after* ownership and status
have been checked, in the same statement that checks them. There is no path by
which text reaches a caller unvalidated, because there is no other place to get
it from.

### 8. The query is normalised NFC, and must fit what the model reads

NFC because that is the form the corpus is in (`pdf_parser.py` normalises
extracted text NFC), so query and documents are compared in one normal form.
Empty and whitespace-only queries are refused rather than embedded — S3.5
already refuses them at the provider, and refusing earlier gives a better
error.

Length is bounded twice: a cheap character cap (`RETRIEVAL_MAX_QUERY_CHARS`)
before tokenizing anything, then the real constraint — the query plus the
model's special tokens must fit `provider.max_input_tokens`. Past that the model
truncates silently and the vector describes a question nobody asked. It is the
same guard S3.5 applies to chunks, applied to queries.

### 9. No HTTP route in S4.1

`MILESTONES.md` defines M4 without a sprint breakdown, so nothing requires the
route now, and `backend/api/routers/search.py` keeps answering 501.

The reason is not caution. The API's `lifespan` deliberately does nothing
(so a momentarily unavailable dependency cannot crash-loop the process), and
S3.5 put the embedding model and the Qdrant client in the **worker's**
composition root. Exposing search over HTTP means deciding whether the API
process loads a 67 MB model and holds a Qdrant connection pool — a real
decision, with a real answer, that no ADR has taken. S4.1 is the layer beneath
it.

### 10. Search is read-only and holds no transaction across a network call

Validate, embed, search Qdrant, **then** open a short read transaction against
PostgreSQL. Repositories still do not commit. Nothing holds a connection while a
model runs or a socket waits.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Trust the Qdrant payload's `user_id` | Skip the database round trip | Exactly what ADR-0003 and principles.md §3 forbid. An index is not an authority, and a payload is only as trustworthy as everything that can write to Qdrant. |
| Compare payload owner against the database | Belt and braces | Sounds stronger, is weaker: it invites the payload into the decision. Reading only the chunk id means there is no comparison to get wrong. |
| One validation query per candidate | Simpler code | N round trips per search, and the ownership predicate written N times. |
| One query by id, ownership checked in Python | Fewer SQL parameters | The unsafe pattern principles.md §3 names: the rows are already read before the check. |
| Return results without a status check | "Vectors exist, so it is searchable" | Vectors outlive documents by design (ADR-0013). This is precisely how a deleted document would keep answering. |
| Rank chunks from a superseded model anyway | More results | Their scores come from a different vector space and are not comparable. A confident wrong answer is worse than a missing one. |
| Treat an unreachable Qdrant as no results | Simpler callers | Turns an outage into a silent, plausible answer. M5 would then cite nothing and sound certain. |
| Re-rank by database order | One less step | Discards the only signal retrieval has. |
| Put chunk text in the payload | One store to read | Reverses ADR-0013 §5 and creates a path to text that has not been validated. |
| Expose `POST /api/v1/search` now | Finish the vertical slice | Requires deciding what the API process loads at startup; ADR-0004/0005/0013 place the model in the worker. A route is cheap to add once that is decided. |

## Consequences

**Good.** Ownership is decided by the database, from the chunk id alone, in one
statement whose predicate cannot be forgotten. A deleted or superseded document
stops being retrievable the moment its row says so, with no reconciliation job
and no vector deletion required. Infrastructure failure is distinguishable from
absence.

**Bad.** Every search pays one PostgreSQL round trip (ADR-0003 predicted and
accepted this). Top-k is measured *before* validation, so a user whose
candidates are mostly stale gets fewer than `top_k` results — the degradation
ADR-0003 §5 describes, now visible. A model change makes every document silently
unsearchable until re-embedded.

**Neutral.** `SearchService` gains no HTTP surface. The stale-vector problem is
not fixed, only rendered harmless; ADR-0003 §6 still awaits the deletion path.

## References

- ADR-0003 §5–§6 — the isolation invariant and the fixed deletion order
- `docs/security/principles.md` §3 — the same invariant as a requirement
- ADR-0013 §3, §5 — `ready` means searchable; no text in the payload
- ADR-0005 — the provider's model and dimension, recorded on every chunk
- `docs/development/retrieval.md` — the implementation
