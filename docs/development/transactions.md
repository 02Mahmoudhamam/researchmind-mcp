# Services and transaction boundaries

```
Router / MCP adapter
        │  Depends(get_document_service)
        ▼
Service                 ← owns the use case, and owns COMMIT
        │  DocumentRepository(session)
        ▼
Repository              ← owns the SQL, flushes, never commits
        │
        ▼
AsyncSession            ← one per request
        │
        ▼
PostgreSQL
```

## The contract

| Question | Answer |
|---|---|
| Who calls `commit()`? | **The service method**, on success |
| Who calls `rollback()`? | The service on failure, and `get_db_session` as a backstop |
| Do repositories commit? | **Never** — asserted by `tests/unit/test_architecture.py` |
| Do repositories flush? | Yes, so constraint violations surface at the operation that caused them |
| Does the request dependency commit? | **No** |
| Multiple repositories, one transaction? | Yes — they share the session they were constructed from |
| Second operation fails? | Nothing persists; the first is rolled back with it |

## Why the service, and not the request

Two reasons, both concrete.

**ADR-0003 fixes an order across two stores:** soft-delete in PostgreSQL,
*committed first*, then delete vectors in Qdrant. The service has to choose when
the PostgreSQL half lands, because the Qdrant call comes after it. A commit in
request teardown could not be sequenced that way.

**FastAPI runs `yield` teardown after the response has started.** Since 0.106,
exceptions raised there no longer reach exception handlers. A commit is the
statement most likely to fail — a constraint violation, a lost connection — and
failing there produces an unhandleable error after a 200 was already promised.

## Why the service rolls back too

`get_db_session` already rolls back on exception, so the service's rollback
looks redundant. It is not: a session left inside a failed transaction rejects
every subsequent statement with `PendingRollbackError`, which is a confusing way
to find out about the *first* failure. Rolling back in the service leaves the
session usable for a caller that catches the error.

`tests/integration/test_transactions.py::TestRollbackAfterFailure
::test_the_session_is_usable_after_the_service_rolls_back` pins this.

## Commit only when something changed

`delete_document` commits only when a row actually matched. A call that deleted
nothing has nothing to make durable, and committing unconditionally would make
any *unrelated* pending work in the session durable as a side effect — which a
test asserts does not happen.

## No Unit of Work class

`AsyncSession` already is one: identity map, dirty tracking, transaction
demarcation. A `UnitOfWork` wrapper would be a second name for the same object
and one more thing to keep correct. The unit of work here is the service method.

This is revisitable if a use case ever needs two service calls to be atomic —
nothing does today, and the shared session already makes multi-repository work
atomic within a single call.

## Verifying a commit in a test

Read it back through a **second session**. An uncommitted transaction can see
its own writes, so reading through the session that wrote proves nothing:

```python
async with get_sessionmaker()() as other:
    stamped = await other.execute(text("SELECT deleted_at FROM documents WHERE id = :id"), ...)
```

Tests that need real commits use the `committing_session` fixture, which
truncates afterwards. `db_session` wraps each test in a transaction and rolls it
back — the right default, but it cannot observe a commit, and a service that
commits inside it would leak rows into the next test.

## Structural invariants

`tests/unit/test_architecture.py` reads source rather than behaviour, because
these properties are structural: a service that opened its own session would
pass every functional test right up until two operations that were supposed to
be atomic turned out not to be. It asserts that no repository commits, rolls
back, or builds a session; that no service builds a session or writes SQL; and
that no *new* FastAPI import appears in the Service Core.

All three were mutation-tested: introducing each violation makes the
corresponding test fail.

### One recorded exemption

`DocumentService.upload_and_process` is typed `file: UploadFile`, which is a
FastAPI type inside the Service Core and contradicts ADR-0001 §2 — the core is
shared with the MCP adapter, which would have to construct one. It is inherited
from the original scaffold, the method is still a stub, and the signature
belongs to **M3**, the sprint that implements it. The exemption is exact: any
other FastAPI import in a service fails, and a second test fails once M3 removes
the debt, so the exemption cannot outlive it.
