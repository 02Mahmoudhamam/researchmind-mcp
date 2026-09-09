# ADR-0006 — A single `ResearchAgent` replacing nine agent packages

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** Milestone M5

## Context

`agents/` contains nine packages — orchestrator, router, summarizer, citation,
semantic_search, research_gap, knowledge_graph, paper_comparison, memory — each
following a four-file pattern.

Verified facts:

- All nine `service.py` files are **identical apart from the class name, import
  paths and two docstring lines**. `diff` shows six hunks, every one an
  identifier substitution.
- All nine `config.py` files are identical apart from the name — including the
  Router, which is a classifier configured with `max_tokens=4096`.
- **Eight of the nine are never instantiated.** Only `OrchestratorAgent` is
  referenced, from `AgentService`.
- Every `run()` returns `AgentOutput(success=True, result=None)` **without
  calling any LLM** — a mock that reports success on the production path.
- `AgentConfig.retry_attempts` and `timeout_seconds` are declared and never
  read; `AgentOutput.tokens_used` is declared and never set.

The documented topology is Orchestrator → Router → seven specialists, which
costs two LLM round-trips of latency and tokens before any useful work begins —
for a single-user research tool.

## Decision

Collapse to a **single `ResearchAgent`** running a Claude tool-use loop over the
MVP capabilities.

1. Create `agents/research/` implementing `BaseAgent`.
2. **Delete the eight unused agent packages.** Byte-identical templates for
   capabilities that do not differ are a maintenance liability and actively
   mislead readers about what the system does.
3. **Keep** `shared/interfaces/agent.py:BaseAgent` and
   `shared/models/agent.py:{AgentInput, AgentOutput, AgentConfig}`. These are
   among the strongest assets in the repository, and they are what makes adding
   a *justified* specialist later a small change rather than a rewrite.
4. The nine system prompts are not wasted: their per-capability guidance moves
   into MCP prompts and per-tool descriptions, which is where MCP intends
   capability-specific instruction to live.
5. **`AgentOutput` must be truthful.** `success=True` requires a completed
   provider response. Failures return `success=False` with a reason;
   `tokens_used` is populated; `retry_attempts` and `timeout_seconds` are
   honoured. Apply prompt caching to the static system prompt.
6. Adding a specialist later is **evidence-gated**: it requires a measurement
   from the M7 evaluation harness showing a distinct need.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Keep all nine and implement each | Implement the documented topology | Nine implementations of one behaviour. Two extra LLM round-trips per request for routing a single-user tool could do statically. |
| Keep Orchestrator + Router, drop specialists | Preserve routing | Retains the latency and cost of routing while removing what it routes to. |
| Delete `BaseAgent` too | Collapse the abstraction entirely | Throws away the seam that makes re-expansion cheap. The interface costs nothing to keep. |

## Consequences

**Good.** One implementation to make correct, observe and test. Two fewer LLM
round-trips per request. The false-success mock is eliminated at its source.
Repository size drops materially and stops advertising capability it lacks.

**Bad.** The repository visibly shrinks, which matters if the multi-agent
structure was part of how the project is presented. Genuinely parallel
multi-agent work would need re-expansion — cheap, because `BaseAgent` remains.

**Neutral.** Capability specialisation moves from Python packages to prompts
and tool descriptions.
