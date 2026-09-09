# ADR-0005 — Embedding dimension derived from the provider abstraction

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0004, ADR-0003, Milestone M4

## Context

`vector_db/qdrant/config.py` declares `vector_size: int = 1536` with the
comment `# text-embedding-3-small dimension`. The value is a literal, bound to
a provider decision that has since changed (ADR-0004 selects a 384-dimension
model).

The failure mode this creates is specific and nasty: a dimension mismatch
between the configured collection and the active provider is **not** caught at
startup. It surfaces as a Qdrant rejection at upsert or query time — that is,
in production traffic, long after deployment, and with an error that points at
the vector store rather than at the configuration that caused it.

Neither `Document` nor `DocumentChunk` records which model produced its
vectors, so after a provider change there is no way to tell which rows are
stale, and no way to drive a targeted re-index from data.

## Decision

The embedding dimension is a **property of the active provider**, never a
literal.

1. Define an `EmbeddingProvider` protocol in `shared/interfaces/` exposing
   `embed_documents()`, `embed_query()`, and — critically — **`dimension` and
   `model_id` as properties**.
2. Qdrant collection creation reads `provider.dimension`. The literal `1536`
   is removed from the codebase entirely.
3. **Startup asserts** that an existing collection's dimension matches
   `provider.dimension`, and fails fast with a remediation message. A
   configuration error must surface at boot, not under traffic.
4. Persist `embedding_model_id` and `dimension` on chunk records (ADR-0003), so
   a model change is *detectable* and re-indexing can be driven from data.
5. Query and document embeddings must use the same provider instance —
   enforced by assertion, not by convention.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Keep the literal, update it on change | Change `1536` to `384` | Moves the bug rather than fixing it. The next provider change reintroduces it, and the runtime-failure mode remains. |
| Environment variable `VECTOR_SIZE` | Operator sets the dimension | Allows the dimension and the model to be configured inconsistently — a new class of misconfiguration. |
| Infer from a probe embedding at startup | Embed a token, measure the vector | Works, but hides the value behind a side effect and costs a model load to learn something the provider already knows. |

## Consequences

**Good.** Switching provider is a configuration change, not a code change.
Mismatches fail at boot with an actionable message. Provenance on chunks makes
re-indexing targeted rather than blind.

**Bad.** One more abstraction to implement before the first embedding is
generated. Startup gains a dependency check that can refuse to boot — correct,
but it must be clearly reported or it will be mistaken for a crash.

**Neutral.** Two providers must be kept conformant to the protocol.
