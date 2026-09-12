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
a stub that never calls an LLM — which is exactly what it was testing. Worse, it
blessed that behaviour: `run()` reports `success=True` with `result=None` and
`tokens_used=0`, and the test gave that a green tick.

Both were deleted in Sprint M0/S0.3. Agent testing returns with M5, against the
agent that actually calls a model.

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

## Deferred behaviour: `xfail(strict=True)`

Functionality a later milestone owns is pinned with a strict xfail, never
deleted and never silenced:

```python
@pytest.mark.xfail(
    strict=True,
    reason="TextChunker.chunk() is a stub returning None. Implemented in "
           "Milestone M3 (Document Ingestion); see ADR-0007.",
)
```

Every xfail must be `strict=True`, carry a precise reason, and name the owning
milestone. Strict matters: when the functionality lands, the test XPASSes and
pytest reports that as a **failure**, so the placeholder is forced to become a
real regression test. A non-strict xfail would go quietly green, which is how
deferred work gets forgotten.

Currently pinned:

| Test | Owner |
|---|---|
| `test_chunker_produces_chunks_covering_the_document` | M3 |
| `test_readiness_reports_dependency_status` | M9 |

## Tests that need PostgreSQL: the `db` marker

Database tests are marked `db` and run against a **real PostgreSQL 16** — never
SQLite. The schema they will carry uses `JSONB`, `UUID` and partial indexes, none
of which SQLite supports, so a passing SQLite run would say nothing about the
code that ships. Mocked repositories would test the mock.

```bash
docker compose up -d postgres     # then the db tests run
poetry run pytest                 # without it they skip, with a reason
```

**The skip is the dangerous part.** A skipped test reports success, so a skip
that quietly becomes permanent is indistinguishable from coverage nobody wrote.
Backend CI therefore sets `REQUIRE_DB=1`, which turns "database unreachable"
into a hard error:

```
ERROR: REQUIRE_DB=1 but PostgreSQL at localhost:5432/researchmind is
unreachable, so 8 database test(s) would be skipped. In CI this is a
failure, not a skip.
```

So the suite stays runnable on a machine with no Docker, and the database
coverage still cannot disappear from CI.

**Each `db` module opts into engine isolation:**

```python
pytestmark = [pytest.mark.db, pytest.mark.usefixtures("engine_isolation")]
```

`get_engine` is cached for the life of the process — correct in production, one
event loop and one pool. pytest-asyncio gives every test its own loop, so a
connection pooled by one test belongs to a loop that is closed by the time the
next test checks it out; with `pool_pre_ping=True` the symptom is
`RuntimeError: Event loop is closed` raised from inside SQLAlchemy's pool. The
fixture disposes the engine between tests. Swapping in `NullPool` for tests
would also work and was rejected: it would mean the pooling configuration that
actually ships is never exercised.

## Commands

```bash
poetry run pytest          # full suite
poetry run pytest -q       # quiet
```

Current state as of Sprint M1/S1.1:

| PostgreSQL | Result |
|---|---|
| running (`docker compose up -d postgres`) | **21 passed, 2 xfailed, 0 failed** |
| not running | **13 passed, 8 skipped, 2 xfailed** — suite still usable |
| not running, `REQUIRE_DB=1` | **hard error** — the CI behaviour |

No collection errors in any of the three. Deterministic — `tests/conftest.py`
supplies settings, so the suite does not depend on a developer's `.env`.
`DATABASE_URL` is the one value it sets with `setdefault` rather than
unconditionally: `db` tests need a genuinely reachable server, so an operator
or CI pointing at their own instance must win.

> On a machine with ROS 2 sourced, `PYTHONPATH` leaks `/opt/ros/*/site-packages`
> into the virtualenv and pytest autoloads ROS plugins that fail on a missing
> `lark`. Run `env -u PYTHONPATH poetry run pytest`. This is a workstation
> issue, not a repository one; CI is unaffected.

## Definition of Done

A sprint is **not** done because files compile, a class exists, a TODO was
removed, a route exists, or a test was written. It is done when the intended
behaviour is genuinely executable and verified by a test that would fail if the
behaviour were removed.
