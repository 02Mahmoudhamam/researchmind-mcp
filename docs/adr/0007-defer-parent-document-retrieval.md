# ADR-0007 — Parent-document retrieval deferred pending evaluation

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0003, Milestones M3, M4, M7

## Context

Parent-document retrieval — embed and search small chunks for precision, then
supply the *enclosing section* to the LLM for coherence — is a strong fit for
this workload. Summarize, compare and gap-detection tasks reason over whole
arguments rather than fragments.

It was proposed as part of the v1 chunking strategy. Two considerations argue
against shipping it in the first slice:

1. The project's own sequencing principle is that tuning must follow
   measurement — re-chunking forces a full re-embed, so guessing is expensive.
   Shipping a tuned configuration first means the MVP baseline *is* the tuned
   configuration, with nothing to compare it against.
2. Returning whole sections rather than chunks materially expands the context
   token budget, and therefore per-query cost and latency — before any cost
   telemetry exists to observe the change.

## Decision

**Defer activation, not capability.**

1. Milestone M3 builds the enabling structure: the `document_chunks` table with
   `section`, `page_start`, `page_end` and ordering, plus section-bounded
   chunking. The parent lookup is cheap because the chunk table exists.
2. Milestone M4 ships **flat chunk retrieval** as the measured baseline.
3. Milestone M7 establishes recall@k, MRR, nDCG, citation resolution rate and
   groundedness against that baseline.
4. Parent-document retrieval is enabled **only if** M7 evidence shows the
   baseline is insufficient — at which point it is a small, evidence-gated
   change rather than a rewrite.

This is a deliberate partial override of the original chunking proposal, which
recommended shipping it in v1.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Ship parent-document retrieval in v1 | Original proposal | The MVP would have no untuned baseline to measure against, and context cost would rise before telemetry exists to see it. |
| Drop it permanently | Flat chunks forever | Discards a technique well matched to the workload. Deferring keeps the option at no cost, because M3 builds the structure anyway. |
| Make it a runtime flag from day one | Ship both, toggle | Two retrieval paths to test and keep correct before either is measured. |

## Consequences

**Good.** M7 measures a genuine baseline. Context budget and cost stay
predictable through the first working slice. The enabling schema is built
regardless, so enabling later is small.

**Bad.** Initial answer quality on synthesis-style questions may be lower than
achievable. If M7 shows it is needed, there is a second round of retrieval work.

**Neutral.** The chunk table carries section metadata from M3 whether or not
the feature is enabled — a modest, deliberate cost.
