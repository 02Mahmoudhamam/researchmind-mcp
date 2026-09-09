# ADR-0002 — REST and MCP as sibling adapters over the Service Core

- **Status:** Accepted
- **Date:** 2026-09-09
- **Related:** ADR-0001, Sprint M0/S0.2, Milestone M6

## Context

The original design had FastAPI calling the MCP server over the MCP protocol
(`docs/architecture/ARCHITECTURE.md`), with an `MCP_SERVER_PORT=8001` and a
compose container for it.

Three verified facts contradict that design:

1. `mcp/server/server.py` implements **stdio** transport
   (`stdio_server()`), while configuration declares an HTTP port and the
   compose container provides neither stdin nor published ports.
2. `ResearchMindMCPClient` exists but has **zero references** anywhere — the
   FastAPI backend never constructs it. The documented edge was never built.
3. The local `mcp/` package **shadows the `mcp` SDK** on `sys.path`, so
   `from mcp.server import Server` raises `ImportError` and `import mcp.types`
   raises `ModuleNotFoundError`. The entire MCP layer is unimportable.

Serializing a request across a process boundary to reach code that is already
importable in the same process buys nothing and costs latency, a failure mode
and a debugging surface.

## Decision

REST and MCP are **sibling adapters over the same Service Core**. Neither calls
the other.

1. Rename `mcp/` to `mcp_server/`, resolving the SDK shadowing. A CI guard
   asserts `mcp.__file__` resolves to site-packages.
2. `mcp_server/tools/*` import and call `backend/services/*` **directly**.
3. The FastAPI backend **never speaks MCP**. Delete `mcp/client/`.
4. Remove the `mcp-server` compose service and `Dockerfile.mcp`. The MCP server
   ships as a documented spawnable stdio command with a Claude Desktop config
   snippet.
5. Remove `MCP_SERVER_HOST` / `MCP_SERVER_PORT` from settings and env examples.
   Dead configuration that contradicts the code is worse than absent
   configuration.
6. Both adapters resolve identity through the **same** resolver, so MCP cannot
   become a path around REST authentication.
7. Streamable HTTP transport is deferred; it may be added later as a second
   transport on the same `Server` object.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| FastAPI → MCP over the protocol (original design) | Backend is an MCP client | Self-RPC. Adds a process hop, serialization and a failure mode to reach in-process code. Nothing depends on it — the client has zero references. |
| MCP server as an HTTP service | Run MCP over Streamable HTTP in a container | Requires an auth story that does not exist yet (ADR pending on tokens). stdio is what Claude Desktop expects and what the code already implements. |
| Duplicate capability logic in each adapter | Independent implementations | Guarantees divergence; two behaviours for one capability. |

## Consequences

**Good.** One implementation, so REST and MCP cannot disagree. No network hop.
The namespace collision — the single defect making the project's namesake
unimportable — is eliminated. Identity is resolved once.

**Bad.** The MCP server is not containerized, so its deployment story differs
from the API's and must be documented separately. The rename touches every
`mcp.*` import in 19 files; it is mechanical but it is a breaking change.

**Neutral.** MCP and REST are versioned and released together, which is correct
while they share an implementation.
