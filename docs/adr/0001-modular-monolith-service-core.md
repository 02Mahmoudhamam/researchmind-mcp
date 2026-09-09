# ADR-0001 — Modular monolith with a shared Service Core

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0002 (adapters), all milestones

## Context

The repository is decomposed into one top-level package per bounded context
(`backend/`, `agents/`, `document_processing/`, `vector_db/`, `memory_system/`,
`shared/`, `mcp/`). The decomposition is sound, but the *behaviour* is almost
entirely unimplemented: 92 TODO-marked empty bodies across 136 files.

Two structural facts shaped this decision:

- `backend/services/` contains exactly one implemented method
  (`AgentService.run`); the capability layer effectively does not exist yet, so
  there is nothing to preserve by choosing differently.
- No file under `agents/` imports anything outside `agents/` and `shared/`.
  The agent layer is fully decoupled — which means integration work has not
  begun, but also that the seams are clean.

The scale target is a single researcher or small group, single-node,
single-tenant. Nothing in the requirements implies independent scaling or
independent deployment of any component.

## Decision

Build a **modular monolith** with a single **Service Core** at
`backend/services/`.

1. `backend/services/*` is the **single implementation** of every capability.
2. Every service method takes an authenticated `Principal`; no adapter type
   (FastAPI `Request`, MCP `arguments` dict) crosses into the core.
3. Infrastructure is reached only through the abstractions in
   `shared/interfaces/` — repository, vector store, embedding provider, LLM
   provider, storage.
4. One deployable backend image; the worker (ADR-0009) shares it.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| Microservices | Separate services for ingest, retrieval, agents | No scaling requirement justifies it. Would add network partitions, distributed transactions and an ops burden to a single-user tool, for zero benefit. |
| Layered monolith without a service core | Logic in routers | Guarantees duplication the moment MCP needs the same capability — the specific failure ADR-0002 exists to prevent. |
| Full hexagonal/ports-and-adapters | Formal port objects, DTO mapping at every boundary | The valuable part — interfaces at infrastructure boundaries — is already present in `shared/interfaces/`. The remaining ceremony costs more than it returns at this size. |

## Consequences

**Good.** One place to implement each capability, so REST and MCP cannot
diverge. Testable core with infrastructure mocked at the interface. A new
engineer reads one package to understand a capability.

**Bad.** All capabilities scale together. A runaway ingest can affect API
latency — mitigated by moving ingestion out of process (ADR-0009). Module
boundaries are convention, enforced by review rather than by the compiler.

**Neutral.** Extracting a service later remains possible precisely because the
interfaces exist; this is not a one-way door.
