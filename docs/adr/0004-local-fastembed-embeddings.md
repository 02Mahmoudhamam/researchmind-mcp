# ADR-0004 — Local FastEmbed embeddings (`bge-small-en-v1.5`)

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0005, Milestone M4

## Context

`document_processing/embedder.py` defaults to OpenAI's
`text-embedding-3-small` in a project whose only declared LLM provider is
Anthropic. Verified problems with that state:

- `openai` is **not a declared dependency**.
- `OPENAI_API_KEY` appears **nowhere** — not in `Settings`, not in
  `.env.example`.
- `self._client = None`, with a TODO naming two mutually exclusive providers
  ("anthropic/openai").
- `vector_db/qdrant/config.py` already hard-codes `vector_size = 1536` to match
  the undecided model.

Anthropic publishes no embeddings endpoint, so this is a genuinely open
decision rather than an oversight to fill in.

The corpus is unpublished academic work — often pre-publication manuscripts.
Milestone M7 will drive repeated re-embedding cycles as chunking is tuned.

## Decision

Adopt **local FastEmbed inference with `BAAI/bge-small-en-v1.5` (384
dimensions)** as the default, behind the provider abstraction of ADR-0005.

1. `FastEmbedProvider` is the default implementation.
2. **Model weights are baked into the Docker image at build time.** FastEmbed
   downloads on first use; an un-baked image fails in any offline or
   air-gapped deployment and makes first-request latency pathological. This is
   the real cost of choosing local and it is paid explicitly.
3. `OpenAIEmbeddingProvider` ships as a documented opt-in behind
   `EMBEDDING_PROVIDER=openai`, so the choice is configuration, not a rewrite.
4. `EMBEDDING_PROVIDER` and `EMBEDDING_MODEL` are added to `Settings` and the
   env example in the same commit as the code that reads them.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| OpenAI `text-embedding-3-small` (1536-d) | Hosted API, current code default | Requires a second API key, contradicting the one-secret goal; sends unpublished manuscripts to a third party; per-token cost on every re-index during M7. |
| Cohere / Voyage embeddings | Hosted, retrieval-tuned | Same third-party and second-key objections, with less ecosystem familiarity. |
| `sentence-transformers` locally | Full PyTorch stack | Much larger image and dependency surface than FastEmbed's ONNX runtime for equivalent quality at this model size. |
| Defer the decision | Keep the stub | The dimension is already hard-coded against it; deferring means M4 blocks on a decision that could be made now. |

## Consequences

**Good.** `docker compose up` requires exactly one secret,
`ANTHROPIC_API_KEY` — the best available contributor experience. No third party
receives users' unpublished work. Re-embedding during evaluation is free, which
matters because M7 will do it repeatedly. `bge-small-en-v1.5` is competitive
with `text-embedding-3-small` on MTEB retrieval, so this is not a quality
trade.

**Bad.** The backend image grows by roughly the size of the ONNX model and its
runtime. Embedding is CPU-bound and slower per document than a hosted API.
Build time increases because weights are fetched at build.

**Neutral.** 384 dimensions rather than 1536 means smaller vectors and a
smaller index — cheaper, and a change that must not be hard-coded anywhere
(ADR-0005).
