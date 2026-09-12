"""Cross-user isolation at the repository boundary.

Separate from tests/integration/test_repositories.py on purpose. These are not
CRUD tests that happen to involve two users — they are the boundary that decides
whether one researcher can read another's unpublished manuscript, and keeping
them together with ordinary behaviour makes it easy to weaken one while reading
the other.

The shape is always the same: two users, each with their own document and
chunks, and every ownership-scoped method called with the *wrong* user.
"""

import uuid
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import (
    DocumentChunkRepository,
    DocumentRepository,
    UserRepository,
)
from shared.models.document import DocumentChunk, DocumentType

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


class Corpus:
    """Two users who must never see each other's data."""

    def __init__(self) -> None:
        self.alice_id = ""
        self.bob_id = ""
        self.alice_document_id = ""
        self.bob_document_id = ""
        self.alice_chunk_id = ""
        self.bob_chunk_id = ""


@pytest.fixture
async def corpus(db_session: AsyncSession) -> Corpus:
    """Alice and Bob, each with one document and one chunk."""
    users = UserRepository(db_session)
    documents = DocumentRepository(db_session)
    chunks = DocumentChunkRepository(db_session)
    fixture = Corpus()

    for name in ("alice", "bob"):
        user = await users.create(
            email=f"{name}-{uuid.uuid4()}@example.com", full_name=name.title()
        )
        document = await documents.create(
            user_id=user.id,
            filename=f"{name}-unpublished.pdf",
            doc_type=DocumentType.PDF,
        )
        stored = await chunks.add_many(
            document_id=document.id,
            user_id=user.id,
            chunks=[
                DocumentChunk(
                    id=str(uuid.uuid4()),
                    document_id=document.id,
                    content=f"{name}'s confidential text",
                    chunk_index=0,
                )
            ],
        )
        setattr(fixture, f"{name}_id", user.id)
        setattr(fixture, f"{name}_document_id", document.id)
        setattr(fixture, f"{name}_chunk_id", stored[0].id)

    return fixture


class TestReadIsolation:
    async def test_alice_cannot_read_bobs_document(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        result = await DocumentRepository(db_session).get_for_user(
            corpus.bob_document_id, corpus.alice_id
        )

        assert result is None

    async def test_bob_cannot_read_alices_document(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Both directions, because a one-sided bug is still a bug."""
        result = await DocumentRepository(db_session).get_for_user(
            corpus.alice_document_id, corpus.bob_id
        )

        assert result is None

    async def test_each_owner_can_still_read_their_own(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """The control. Without it, a repository that returns None always passes."""
        repo = DocumentRepository(db_session)

        assert await repo.get_for_user(corpus.alice_document_id, corpus.alice_id)
        assert await repo.get_for_user(corpus.bob_document_id, corpus.bob_id)

    async def test_a_foreign_document_is_indistinguishable_from_a_missing_one(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Otherwise the return value is an existence oracle.

        If "someone else's" and "no such id" differed, an attacker could
        enumerate which document ids exist in other people's corpora.
        """
        repo = DocumentRepository(db_session)

        foreign = await repo.get_for_user(corpus.bob_document_id, corpus.alice_id)
        absent = await repo.get_for_user(str(uuid.uuid4()), corpus.alice_id)

        assert foreign == absent is None


class TestListIsolation:
    async def test_each_list_contains_only_its_owners_documents(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        repo = DocumentRepository(db_session)

        alice = await repo.list_for_user(corpus.alice_id)
        bob = await repo.list_for_user(corpus.bob_id)

        assert [d.id for d in alice] == [corpus.alice_document_id]
        assert [d.id for d in bob] == [corpus.bob_document_id]
        assert {d.user_id for d in alice} == {corpus.alice_id}
        assert {d.user_id for d in bob} == {corpus.bob_id}

    async def test_no_filename_leaks_across_the_boundary(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Even a filename is disclosure — "bob-unpublished.pdf" says plenty."""
        alice = await DocumentRepository(db_session).list_for_user(corpus.alice_id)

        assert all("bob" not in d.filename for d in alice)


class TestDeleteIsolation:
    async def test_alice_cannot_delete_bobs_document(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        result = await DocumentRepository(db_session).soft_delete_for_user(
            corpus.bob_document_id, corpus.alice_id
        )

        assert result is False

    async def test_a_refused_delete_leaves_the_target_untouched(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Not enough that it returns False — the row must be unchanged.

        Read back outside the repository, so a bug in the ownership-scoped read
        cannot hide a mutation the ownership-scoped write made.
        """
        repo = DocumentRepository(db_session)
        await repo.soft_delete_for_user(corpus.bob_document_id, corpus.alice_id)

        row = await db_session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(corpus.bob_document_id)},
        )

        assert row.scalar_one() is None, "Alice's delete stamped Bob's document"
        assert await repo.get_for_user(corpus.bob_document_id, corpus.bob_id)

    async def test_deleting_with_the_wrong_owner_does_not_delete_anything_else(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """A missing ownership predicate would match by id alone.

        Alice's own document must also survive an attempt aimed at Bob's.
        """
        repo = DocumentRepository(db_session)
        await repo.soft_delete_for_user(corpus.bob_document_id, corpus.alice_id)

        assert len(await repo.list_for_user(corpus.alice_id)) == 1
        assert len(await repo.list_for_user(corpus.bob_id)) == 1


class TestChunkIsolation:
    async def test_alice_cannot_read_bobs_chunk(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Ownership reaches the chunk only through the document join."""
        result = await DocumentChunkRepository(db_session).get_for_user(
            corpus.bob_chunk_id, corpus.alice_id
        )

        assert result is None

    async def test_bob_cannot_read_alices_chunk(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        result = await DocumentChunkRepository(db_session).get_for_user(
            corpus.alice_chunk_id, corpus.bob_id
        )

        assert result is None

    async def test_each_owner_can_still_read_their_own_chunk(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        repo = DocumentChunkRepository(db_session)

        assert await repo.get_for_user(corpus.alice_chunk_id, corpus.alice_id)
        assert await repo.get_for_user(corpus.bob_chunk_id, corpus.bob_id)

    async def test_alice_cannot_list_bobs_chunks(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        result = await DocumentChunkRepository(db_session).list_for_document(
            corpus.bob_document_id, corpus.alice_id
        )

        assert result == []

    async def test_alice_cannot_write_chunks_into_bobs_document(
        self, db_session: AsyncSession, corpus: Corpus
    ) -> None:
        """Injecting text into someone else's corpus would poison their answers.

        Their retrieval would surface it and their agent would cite it.
        """
        repo = DocumentChunkRepository(db_session)

        with pytest.raises(LookupError):
            await repo.add_many(
                document_id=corpus.bob_document_id,
                user_id=corpus.alice_id,
                chunks=[
                    DocumentChunk(
                        id=str(uuid.uuid4()),
                        document_id=corpus.bob_document_id,
                        content="injected",
                        chunk_index=99,
                    )
                ],
            )

        assert (
            len(await repo.list_for_document(corpus.bob_document_id, corpus.bob_id))
            == 1
        )


@pytest.fixture
async def executed_sql(engine_isolation: None) -> AsyncIterator[list[str]]:
    """Record every statement the database actually executes.

    Depends on `engine_isolation` so the listener attaches to the same engine
    the test will use.
    """
    from backend.db.engine import get_engine

    statements: list[str] = []

    def record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


class TestOwnershipIsInTheQuery:
    """The regression this sprint most needs to prevent.

    Someone replaces an ownership-scoped query with a lookup by id plus a Python
    check, the behavioural tests above still pass — because the Python check is
    there — and then a later refactor drops the check. These tests read the SQL
    that PostgreSQL received, so the predicate has to be in the query itself.
    """

    async def test_document_read_filters_by_owner_in_sql(
        self, db_session: AsyncSession, corpus: Corpus, executed_sql: list[str]
    ) -> None:
        executed_sql.clear()
        await DocumentRepository(db_session).get_for_user(
            corpus.alice_document_id, corpus.alice_id
        )

        selects = [s for s in executed_sql if "FROM documents" in s]
        assert selects, "no query against documents was executed"
        assert all("documents.user_id = " in s for s in selects), selects
        assert all("documents.deleted_at IS NULL" in s for s in selects), selects

    async def test_document_list_filters_by_owner_in_sql(
        self, db_session: AsyncSession, corpus: Corpus, executed_sql: list[str]
    ) -> None:
        executed_sql.clear()
        await DocumentRepository(db_session).list_for_user(corpus.alice_id)

        selects = [s for s in executed_sql if "FROM documents" in s]
        assert selects
        assert all("documents.user_id = " in s for s in selects), selects

    async def test_soft_delete_filters_by_owner_in_sql(
        self, db_session: AsyncSession, corpus: Corpus, executed_sql: list[str]
    ) -> None:
        """The UPDATE must carry the owner, not just the id."""
        executed_sql.clear()
        await DocumentRepository(db_session).soft_delete_for_user(
            corpus.alice_document_id, corpus.alice_id
        )

        updates = [s for s in executed_sql if s.strip().upper().startswith("UPDATE")]
        assert updates, "no UPDATE was executed"
        assert all("documents.user_id = " in s for s in updates), updates

    async def test_chunk_read_joins_documents_for_ownership_in_sql(
        self, db_session: AsyncSession, corpus: Corpus, executed_sql: list[str]
    ) -> None:
        """Proves the join, not a second query plus a Python comparison."""
        executed_sql.clear()
        await DocumentChunkRepository(db_session).get_for_user(
            corpus.alice_chunk_id, corpus.alice_id
        )

        selects = [s for s in executed_sql if "FROM document_chunks" in s]
        assert selects, "no query against document_chunks was executed"
        assert all("JOIN documents" in s for s in selects), selects
        assert all("documents.user_id = " in s for s in selects), selects
