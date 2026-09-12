# Repositories and ownership

The repositories in `backend/db/repositories/` are the only place the
application reaches PostgreSQL. Their job is not to hide SQL — it is to make
the ownership-safe query the one that is easy to write.

## The rule

> **A method that can reach a user-owned row takes `user_id`, and that
> `user_id` goes into the SQL.**

`docs/security/principles.md` §3 states the requirement: the ownership filter is
"a **required parameter of the repository signature**, not an optional `filters`
dict entry", and "the safe path must be the only path".

So this exists:

```python
await documents.get_for_user(document_id, user_id)
```

and this deliberately does not:

```python
await documents.get_by_id(document_id)   # no such method
```

### Why the predicate, not a check

These are not equivalent:

```python
# what the repositories do
select(DocumentORM).where(
    DocumentORM.id == target,
    DocumentORM.user_id == owner,
    DocumentORM.deleted_at.is_(None),
)

# what they must never become
row = await session.get(DocumentORM, target)
if row.user_id != owner:          # one `if` away from a breach
    return None
```

The second version loads someone else's row into the process before deciding
the caller may not have it, and the check is a line a refactor can delete. The
behavioural isolation tests **cannot tell the two apart** — that was measured,
not assumed: mutating the repository to the unsafe form leaves every
read-isolation test passing.

`tests/integration/test_repository_security.py::TestOwnershipIsInTheQuery`
records the SQL PostgreSQL actually received and asserts the predicate is in it.
That is the test which fails on the mutation, and it is why it exists.

## Method inventory

| Repository | Method | Owner required? |
|---|---|---|
| `UserRepository` | `create` | n/a — a user has no owner |
| | `get_by_id` | no — **User is the ownership root** |
| | `get_by_email` | no — the M2 login lookup |
| `DocumentRepository` | `create(user_id=…)` | ✅ the owner being assigned |
| | `get_for_user(document_id, user_id)` | ✅ in the `WHERE` |
| | `list_for_user(user_id)` | ✅ in the `WHERE` |
| | `soft_delete_for_user(document_id, user_id)` | ✅ in the `UPDATE … WHERE` |
| `DocumentChunkRepository` | `add_many(document_id, user_id, …)` | ✅ document ownership resolved first |
| | `get_for_user(chunk_id, user_id)` | ✅ via `JOIN documents` |
| | `list_for_document(document_id, user_id)` | ✅ via `JOIN documents` |

**`UserRepository.get_by_id` is the only ID-only accessor**, and it is safe
because a user is not a user-owned resource — it is the root every other
ownership check terminates at. M2's `get_current_user` resolves a verified token
subject through it.

A chunk has no owner column. Ownership reaches it through its document:

```sql
FROM document_chunks
JOIN documents ON document_chunks.document_id = documents.id
WHERE document_chunks.id = :chunk_id
  AND documents.user_id   = :user_id
  AND documents.deleted_at IS NULL
```

## Not-found semantics

Reads return `None` or `[]`. Deletes return `False`.

**All three of these look identical to a caller**: the row does not exist, the
row belongs to someone else, the row is soft-deleted. That is deliberate — a
caller able to distinguish them would be an existence oracle for other people's
corpora, and "which document ids exist in Bob's account" is not a question the
API should answer.

The one asymmetry: `add_many` **raises `LookupError`** when the target document
is not an owned, live document. A read finding nothing is ordinary; a write
that cannot be attributed to an owned document is a bug, and returning an empty
list would be another "reported success, did nothing" result. `LookupError`
rather than a bespoke exception — the stdlib already means this, and a new error
hierarchy is not something a repository layer should invent.

## Soft delete

`soft_delete_for_user` stamps `deleted_at` with the **database server's**
`now()`, and is the only delete offered. Hard deletion is not exposed: ADR-0003
fixes the order as soft-delete in PostgreSQL, committed first, then delete
vectors in Qdrant. Removing the row now would orphan vectors with nothing left
to reconcile against.

After a soft delete the row and its chunks remain in the database, and every
ownership-scoped read stops returning them — including `list_for_document` for
the chunks underneath. Deleting twice returns `False` the second time, because
the `UPDATE` carries `deleted_at IS NULL` and matches nothing.

There is no restore path. Nothing in the architecture asks for one yet.

## Transactions

Repositories **flush; they never commit.** The caller owns the transaction,
because ADR-0003's deletion order spans PostgreSQL and Qdrant and the service
has to choose when the PostgreSQL half lands. `get_db_session` guarantees
rollback and close, and also does not commit.

The full contract — who commits, what happens when a later operation fails, and
how to verify a commit in a test — is in
[transactions.md](transactions.md).

## Return types

Repositories return the Pydantic models from `shared/models/`, never ORM rows.
`DocumentService` already declares `-> Document | None` and `-> list[Document]`,
so this is the boundary the architecture already expected rather than a layer
invented here. It also means no caller can hold a detached ORM instance and hit
a lazy-load error far from its cause.

Two persistence-only columns have no domain field: `document_chunks
.embedding_model_id` and `.dimension`. M4 reads them directly when deciding what
to re-index.

## Why there is no `BaseRepository`

`shared/interfaces/repository.py` defines a generic ABC with `get_by_id`,
`get_all`, `create`, `update` and `delete` — every one reaching a row by id
alone. `get_all()` cannot be made ownership-safe at all.

None of the three repositories inherit it, and the file now carries a warning
saying so. This partially supersedes **ADR-0003 §3**, which asked for them to be
implementations of that ABC: `principles.md` is labelled non-negotiable and an
ADR is by construction revisitable, so the invariant wins. The ABC has zero
implementations and zero callers, so nothing depended on the decision either
way.
