"""Transaction boundaries against real PostgreSQL.

The contract these tests pin down:

    repository   add / execute / flush        — never commits
    service      commit on success,
                 rollback and re-raise on failure
    dependency   rollback on exception, close always — never commits

Durability is checked from a **second session** wherever it matters. Reading
back through the session that wrote is not evidence of a commit: an uncommitted
transaction can see its own writes.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import DocumentRepository, UserRepository
from backend.services.document_service import DocumentService
from shared.models.document import DocumentType

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


async def _in_a_separate_session(sql: str, params: dict[str, object]) -> object:
    """Read through a connection that shares nothing with the test's session."""
    from backend.db.session import get_sessionmaker

    async with get_sessionmaker()() as other:
        result = await other.execute(text(sql), params)
        return result.scalar_one_or_none()


async def _email() -> str:
    return f"{uuid.uuid4()}@example.com"


class TestSuccessfulCommit:
    async def test_a_committed_delete_is_visible_to_another_session(
        self, committing_session: AsyncSession
    ) -> None:
        """The service owns the commit, and the commit is real."""
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )
        document = await DocumentRepository(committing_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        await committing_session.commit()

        deleted = await DocumentService(committing_session).delete_document(
            document.id, user.id
        )

        assert deleted is True
        stamped = await _in_a_separate_session(
            "SELECT deleted_at FROM documents WHERE id = :id",
            {"id": uuid.UUID(document.id)},
        )
        assert stamped is not None, "the delete was not committed"

    async def test_a_delete_that_matched_nothing_commits_nothing(
        self, committing_session: AsyncSession
    ) -> None:
        """No mutation, nothing to make durable.

        The uncommitted user below proves it: if the service had committed
        unconditionally, that row would survive into the second session.
        """
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )

        deleted = await DocumentService(committing_session).delete_document(
            str(uuid.uuid4()), user.id
        )

        assert deleted is False
        leaked = await _in_a_separate_session(
            "SELECT id FROM users WHERE id = :id", {"id": uuid.UUID(user.id)}
        )
        assert leaked is None, "an unrelated pending row was committed"


class TestRollbackAfterFailure:
    async def test_a_failure_during_commit_rolls_the_mutation_back(
        self, committing_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation happens, the commit fails, nothing survives.

        Simulates the realistic version — a lost connection or a deferred
        constraint firing at commit time — rather than a contrived error before
        any work is done.
        """
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )
        document = await DocumentRepository(committing_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        await committing_session.commit()

        async def exploding_commit() -> None:
            raise RuntimeError("connection lost at commit")

        monkeypatch.setattr(committing_session, "commit", exploding_commit)

        with pytest.raises(RuntimeError):
            await DocumentService(committing_session).delete_document(
                document.id, user.id
            )

        monkeypatch.undo()
        stamped = await _in_a_separate_session(
            "SELECT deleted_at FROM documents WHERE id = :id",
            {"id": uuid.UUID(document.id)},
        )
        assert stamped is None, "a failed commit left the document deleted"

    async def test_the_session_is_usable_after_the_service_rolls_back(
        self, committing_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the service rolls back rather than leaving it to teardown.

        A session left inside a failed transaction rejects every later statement
        with PendingRollbackError, which is a confusing way to find out about
        the *first* failure.
        """
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )
        document = await DocumentRepository(committing_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        await committing_session.commit()

        async def exploding_commit() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(committing_session, "commit", exploding_commit)
        with pytest.raises(RuntimeError):
            await DocumentService(committing_session).delete_document(
                document.id, user.id
            )
        monkeypatch.undo()

        result = await committing_session.execute(text("SELECT 1"))

        assert result.scalar_one() == 1


class TestDatabaseFailure:
    async def test_a_constraint_violation_leaves_nothing_behind(
        self, committing_session: AsyncSession
    ) -> None:
        """A repository failure must not half-persist the work before it."""
        repo = UserRepository(committing_session)
        email = await _email()
        first = await repo.create(email=email, full_name="First")

        with pytest.raises(IntegrityError):
            await repo.create(email=email, full_name="Duplicate")

        await committing_session.rollback()
        survivor = await _in_a_separate_session(
            "SELECT id FROM users WHERE id = :id", {"id": uuid.UUID(first.id)}
        )
        assert survivor is None, "the first insert survived a rolled-back unit"


class TestMultiRepositoryAtomicity:
    """Two repositories, one transaction.

    No service method in S1.4 spans two repositories — `delete_document` touches
    one, and the second store in ADR-0003's deletion order is Qdrant, which is
    M4. Rather than invent a workflow to justify a test, this exercises the
    boundary directly: the property under test is that repositories share the
    caller's session and therefore its transaction, which is what makes a future
    multi-repository use case atomic by construction.
    """

    async def test_both_operations_persist_when_both_succeed(
        self, committing_session: AsyncSession
    ) -> None:
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )
        document = await DocumentRepository(committing_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )

        await committing_session.commit()

        assert await _in_a_separate_session(
            "SELECT id FROM users WHERE id = :id", {"id": uuid.UUID(user.id)}
        )
        assert await _in_a_separate_session(
            "SELECT id FROM documents WHERE id = :id",
            {"id": uuid.UUID(document.id)},
        )

    async def test_neither_persists_when_the_second_fails(
        self, committing_session: AsyncSession
    ) -> None:
        """UserRepository succeeds, DocumentRepository violates the foreign key.

        The user must not survive. If repositories committed independently it
        would, and a half-written unit of work is exactly what the contract
        forbids.
        """
        user = await UserRepository(committing_session).create(
            email=await _email(), full_name="Ada"
        )

        with pytest.raises(IntegrityError):
            await DocumentRepository(committing_session).create(
                user_id=str(uuid.uuid4()),  # no such user
                filename="orphan.pdf",
                doc_type=DocumentType.PDF,
            )
        await committing_session.rollback()

        survivor = await _in_a_separate_session(
            "SELECT id FROM users WHERE id = :id", {"id": uuid.UUID(user.id)}
        )
        assert survivor is None, "the user persisted despite the unit failing"

    async def test_repositories_built_from_one_session_share_its_transaction(
        self, committing_session: AsyncSession
    ) -> None:
        """The mechanism behind the two tests above, asserted directly."""
        users = UserRepository(committing_session)
        documents = DocumentRepository(committing_session)

        assert users._session is documents._session is committing_session
