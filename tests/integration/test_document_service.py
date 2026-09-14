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
from shared.models.principal import Principal

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


async def _user(session: AsyncSession, name: str = "Ada") -> Principal:
    """A real user, returned as the Principal the service receives (M2/S2.5).

    Built from the created row rather than invented, so the identity handed to
    the service is one that actually exists. Only `user_id` reaches the SQL.
    """
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name=name
    )
    return Principal(user_id=user.id, email=user.email, role=user.role)


async def _document(session: AsyncSession, owner: Principal, filename: str) -> str:
    document = await DocumentRepository(session).create(
        user_id=owner.user_id, filename=filename, doc_type=DocumentType.PDF
    )
    return document.id


class TestReads:
    async def test_get_returns_the_owned_document(
        self, committing_session: AsyncSession
    ) -> None:
        owner = await _user(committing_session)
        document_id = await _document(committing_session, owner, "paper.pdf")
        service = DocumentService(committing_session)

        found = await service.get_document(document_id, owner)

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
        owner = await _user(committing_session)
        document_id = await _document(committing_session, owner, "p.pdf")
        service = DocumentService(committing_session)

        found = await service.get_document(document_id, owner)

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
        owner = await _user(committing_session)
        document_id = await _document(committing_session, owner, "d.pdf")
        service = DocumentService(committing_session)

        assert await service.delete_document(document_id, owner) is True

        assert await service.get_document(document_id, owner) is None
        assert await service.list_user_documents(owner) == []

    async def test_delete_is_soft_and_the_row_survives(
        self, committing_session: AsyncSession
    ) -> None:
        """ADR-0003 needs the row: M4 reconciles Qdrant against it."""
        owner = await _user(committing_session)
        document_id = await _document(committing_session, owner, "d.pdf")

        await DocumentService(committing_session).delete_document(document_id, owner)

        row = await committing_session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(document_id)},
        )
        assert row.scalar_one() is not None

    async def test_deleting_twice_reports_false_the_second_time(
        self, committing_session: AsyncSession
    ) -> None:
        owner = await _user(committing_session)
        document_id = await _document(committing_session, owner, "d.pdf")
        service = DocumentService(committing_session)

        assert await service.delete_document(document_id, owner) is True
        assert await service.delete_document(document_id, owner) is False


class TestUploadWiring:
    async def test_upload_refuses_to_run_without_storage_and_a_queue(
        self, committing_session: AsyncSession
    ) -> None:
        """Replaces `test_upload_is_still_a_stub`, which M1 pinned for M3 to retire.

        Upload is real now (M3/S3.1), so the pin that said "wired up is not
        uploads work" has done its job. What remains true for a service built
        for reads alone is that it refuses to upload — before reading the
        stream, so nothing is consumed and nothing is stored.
        """
        import io

        from backend.services.document_service import UploadNotConfigured

        service = DocumentService(committing_session)
        owner = await _user(committing_session)
        stream = io.BytesIO(b"%PDF-never-read")

        with pytest.raises(UploadNotConfigured):
            await service.upload_and_process(
                filename="p.pdf", stream=stream, principal=owner
            )
        assert stream.tell() == 0
