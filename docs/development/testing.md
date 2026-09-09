# Testing Strategy

## Principle

**A test that cannot fail is worse than no test.** It costs maintenance and
creates false confidence.

The repository baseline demonstrates why this matters. Two of its four tests
passed — and both were vacuous:

```python
result = await agent.run(sample_input)
assert isinstance(result, AgentOutput)
assert result.agent_name == "summarizer"
```

This passes *because* the agent does nothing. It would pass identically against
a stub that never calls an LLM — which is exactly what it was testing. Both
were deleted rather than kept.

## Test levels

| Level | Location | Runs against | Purpose |
|---|---|---|---|
| Unit | `tests/unit/` | Mocks at interface boundaries | Logic in isolation |
| Integration | `tests/integration/` | **Real** Postgres + Qdrant via testcontainers | Adapters, repositories, API |
| End-to-end | `tests/e2e/` | Full compose stack | Complete user journeys |
| Evaluation | `evaluation/` | Golden dataset | Retrieval and answer quality |

Integration tests use real services rather than mocks. A mocked vector store
cannot demonstrate that a tenant filter works, and that is the property that
matters most.

## Release-gate suites

These are blocking. A milestone does not merge if any of them fails.

**Authentication (M2).** Missing header; malformed token; forged token; expired
token; wrong signing secret; `alg: none`; token for a deleted or inactive user;
under-privileged role. Each must return 401 or 403 — never 200.

**Tenant isolation (M4, extended in M6).** User A must not reach user B's data
by query, by forged `document_ids`, by a body-supplied `user_id`, or through
any MCP tool or resource. A chunk deleted in Postgres but orphaned in Qdrant
must be dropped at validation and never returned.

**Agent honesty (M5).** A forced provider failure must yield `success=False`.
A static check asserts no unconditional `success=True` in `agents/`.

**Citation integrity (M5, measured in M7).** Every citation marker must resolve
to a chunk that was actually supplied. Zero retrieval must produce an explicit
"no relevant sources" answer with **no LLM call made**.

## Commands

Test commands are documented here as each milestone establishes them. At the
repository baseline the suite does not run — collection fails on missing
dependencies, and one test fails by construction against an unimplemented
chunker. Sprint M0/S0.1 is what makes `pytest` collect cleanly.

## Definition of Done

A sprint is **not** done because files compile, a class exists, a TODO was
removed, a route exists, or a test was written. It is done when the intended
behaviour is genuinely executable and verified by a test that would fail if the
behaviour were removed.
