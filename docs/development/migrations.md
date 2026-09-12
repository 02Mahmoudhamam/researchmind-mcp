# Database migrations

The schema is owned by Alembic. Nothing else creates tables — not application
startup, not the test suite, not `Base.metadata.create_all()`. A guard in
`tests/unit/test_db_infrastructure.py` fails the build if schema creation
appears anywhere under `backend/` or in `main.py`.

## Everyday commands

```bash
docker compose up -d postgres        # the database must be running first

poetry run alembic current           # which revision this database is at
poetry run alembic heads             # the latest revision available
poetry run alembic history           # the full chain
poetry run alembic upgrade head      # apply everything outstanding
poetry run alembic downgrade base    # back to an empty database
poetry run alembic upgrade head --sql  # print the SQL instead of running it
```

`alembic` reads `DATABASE_URL` from `Settings`, so it always talks to the same
database the application does. `sqlalchemy.url` in `alembic.ini` is deliberately
empty — a second source of configuration is a second thing to drift.

## Writing a migration

```bash
poetry run alembic revision --autogenerate -m "what changed"
poetry run black alembic/versions/          # generated files are not black-clean
```

Then **read it**. Autogenerate is a drafting tool, not an author:

- It misses `CHECK` constraints, partial-index predicates and server defaults
  unless `compare_type` and `compare_server_default` are on — both are enabled
  in `alembic/env.py`.
- It proposes dropping any table it cannot see. A model that is not imported in
  `backend/db/models/__init__.py` is invisible to it.
- It does not know your intent for `ondelete`.

Check before committing that every constraint and index is named via `op.f()`,
that `downgrade()` reverses `upgrade()` exactly, and that the diff contains
nothing you did not ask for.

**Revision ids are sequential and zero-padded** — `0001`, `0002`, … — so
`alembic downgrade 0003` is something you can type. Filenames are date-prefixed
by `file_template` in `alembic.ini`, so `ls alembic/versions` reads in order.

## Verifying a migration

A migration that cannot round-trip is not reviewable: there is no way to try it,
back it out and try again.

```bash
poetry run alembic upgrade head
poetry run alembic check              # models and database agree
poetry run alembic downgrade base
poetry run alembic upgrade head
```

Run the migration tests, which do the same cycle and then inspect
`pg_catalog` — constraint names, foreign-key delete actions, the partial index
predicate, and the absence of orphaned enum types:

```bash
poetry run pytest tests/integration/test_migrations.py -v
```

Backend CI runs both the cycle and these tests against a real `postgres:16`
service container, with `REQUIRE_DB=1` so an unreachable database fails rather
than skips.

## Two things that will bite

**Never use a native PostgreSQL `ENUM`.** `drop_table` leaves the type behind,
so `downgrade` succeeds while the database is not actually empty and the next
`upgrade` fails on a duplicate type. The enum columns here are `VARCHAR` with a
`CHECK` constraint (`native_enum=False, create_constraint=True`), which an
ordinary `ALTER TABLE` can rewrite. Note `create_constraint` defaults to
**False** — without it you get a bare `VARCHAR` that accepts any string.

**Never rely on auto-generated constraint names.** The naming convention on
`Base.metadata` exists so `downgrade()` has a name to drop things by. It has to
be in place before the first autogenerate, and changing it later means renaming
constraints on a populated schema.

## Deployment

Migrations run as an explicit step, never on application boot. Replicas racing
`upgrade head` at startup is a real failure mode, and a web process that
migrates as it starts cannot be rolled back independently of its schema.
