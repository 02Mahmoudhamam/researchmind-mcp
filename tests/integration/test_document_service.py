"""DocumentService against real PostgreSQL.

Service-layer tests, not repository tests: the question here is whether the
service orchestrates correctly and whether ownership context reaches the
repository intact. Whether the repository's SQL carries the predicate is
tests/integration/test_repository_security.py.

Nothing is mocked. A mocked repository would prove the service calls a method,
which is the part that was never in doubt.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import DocumentRepository, UserRepository
from backend.services.document_service import DocumentService
from shared.models.document import Document, DocumentMetadata, DocumentType

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


async def _user(session: AsyncSession, name: str = "Ada") -> str:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name=name
    )
    return user.id


async def _document(session: AsyncSession, user_id: str, filename: str) -> str:
    document = await DocumentRepository(session).create(
        user_id=user_id, filename=filename, doc_type=DocumentType.PDF
    )
    return document.id


class TestReads:
    async def test_get_returns_the_owned_document(
        self, committing_session: AsyncSession
    ) -> None:
        user_id = await _user(committing_session)
        document_id = await _document(committing_session, user_id, "paper.pdf")
        service = DocumentService(committing_session)

        found = await service.get_document(document_id, user_id)

        assert isinstance(found, Document)
        assert found.filename == "paper.pdf"

    async def test_list_returns_only_this_users_documents(
        self, committing_session: AsyncSession
    ) -> None:
        alice = await _user(committing_session, "Alice")
        bob = await _user(committing_session, "Bob")
        await _document(committing_session, alice, "alice.pdf")
        await _document(committing_session, bob, "bob.pdf")
        service = DocumentService(committing_session)

        listed = await service.list_user_documents(alice)

        assert [d.filename for d in listed] == ["alice.pdf"]

    async def test_reads_return_domain_models_not_orm_rows(
        self, committing_session: AsyncSession
    ) -> None:
        """The API/domain/ORM boundary survives the service layer."""
        user_id = await _user(committing_session)
        document_id = await _document(committing_session, user_id, "p.pdf")
        service = DocumentService(committing_session)

        found = await service.get_document(document_id, user_id)

        assert found is not None
        assert not hasattr(found, "_sa_instance_state")
        assert isinstance(found.metadata, DocumentMetadata)


class TestOwnershipPropagation:
    """The service must not become a way around S1.3."""

    async def test_get_refuses_another_users_document(
        self, committing_session: AsyncSession
    ) -> None:
        alice = await _user(committing_session, "Alice")
        bob = await _user(committing_session, "Bob")
        bobs_document = await _document(committing_session, bob, "bob-secret.pdf")
        service = DocumentService(committing_session)

        assert await service.get_document(bobs_document, alice) is None

    async def test_delete_refuses_another_users_document(
        self, committing_session: AsyncSession
    ) -> None:
        alice = await _user(committing_session, "Alice")
        bob = await _user(committing_session, "Bob")
        bobs_document = await _document(committing_session, bob, "bob-secret.pdf")
        service = DocumentService(committing_session)

        deleted = await service.delete_document(bobs_document, alice)

        assert deleted is False

    async def test_a_refused_delete_does_not_touch_the_target(
        self, committing_session: AsyncSession
    ) -> None:
        """Checked with raw SQL, so a bug in the scoped read cannot hide it."""
        alice = await _user(committing_session, "Alice")
        bob = await _user(committing_session, "Bob")
        bobs_document = await _document(committing_session, bob, "bob-secret.pdf")
        await committing_session.commit()

        await DocumentService(committing_session).delete_document(bobs_document, alice)

        row = await committing_session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(bobs_document)},
        )
        assert row.scalar_one() is None

    async def test_a_foreign_document_looks_exactly_like_a_missing_one(
        self, committing_session: AsyncSession
    ) -> None:
        alice = await _user(committing_session, "Alice")
        bob = await _user(committing_session, "Bob")
        bobs_document = await _document(committing_session, bob, "bob.pdf")
        service = DocumentService(committing_session)

        foreign = await service.get_document(bobs_document, alice)
        absent = await service.get_document(str(uuid.uuid4()), alice)

        assert foreign == absent is None


class TestSoftDelete:
    async def test_delete_hides_the_document_from_both_read_paths(
        self, committing_session: AsyncSession
    ) -> None:
        user_id = await _user(committing_session)
        document_id = await _document(committing_session, user_id, "d.pdf")
        service = DocumentService(committing_session)

        assert await service.delete_document(document_id, user_id) is True

        assert await service.get_document(document_id, user_id) is None
        assert await service.list_user_documents(user_id) == []

    async def test_delete_is_soft_and_the_row_survives(
        self, committing_session: AsyncSession
    ) -> None:
        """ADR-0003 needs the row: M4 reconciles Qdrant against it."""
        user_id = await _user(committing_session)
        document_id = await _document(committing_session, user_id, "d.pdf")

        await DocumentService(committing_session).delete_document(document_id, user_id)

        row = await committing_session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(document_id)},
        )
        assert row.scalar_one() is not None

    async def test_deleting_twice_reports_false_the_second_time(
        self, committing_session: AsyncSession
    ) -> None:
        user_id = await _user(committing_session)
        document_id = await _document(committing_session, user_id, "d.pdf")
        service = DocumentService(committing_session)

        assert await service.delete_document(document_id, user_id) is True
        assert await service.delete_document(document_id, user_id) is False


class TestNotImplementedSurface:
    async def test_upload_is_still_a_stub(
        self, committing_session: AsyncSession
    ) -> None:
        """S1.4 integrates persistence, not ingestion.

        Pinned so that "the service is wired up" is never read as "uploads
        work". Storage is ADR-0008 and the pipeline is ADR-0009, both M3.
        """
        service = DocumentService(committing_session)

        assert await service.upload_and_process(None, "irrelevant") is None  # type: ignore[arg-type]
