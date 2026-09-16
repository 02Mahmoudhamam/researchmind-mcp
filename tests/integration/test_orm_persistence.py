"""What PostgreSQL actually enforces.

tests/unit/test_orm_models.py asserts what the ORM declares. This asserts the
database refuses the things it should — a declaration that is not enforced is
documentation, and ADR-0003's whole argument for PostgreSQL over Qdrant is that
ownership becomes a constraint rather than a convention.

Ownership *authorisation* — who may read what — is Sprint M1/S1.3. Nothing here
tests authorisation; it tests structure.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DocumentChunkORM, DocumentORM, UserORM
from shared.models.document import DocumentStatus, DocumentType
from shared.models.user import UserRole

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


def _user(**overrides: object) -> UserORM:
    defaults: dict[str, object] = {
        "email": f"{uuid.uuid4()}@example.test",
        "full_name": "Ada Lovelace",
    }
    return UserORM(**{**defaults, **overrides})


def _document(user: UserORM, **overrides: object) -> DocumentORM:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "filename": "paper.pdf",
        "doc_type": DocumentType.PDF,
    }
    return DocumentORM(**{**defaults, **overrides})


class TestDefaults:
    async def test_ids_are_generated_by_python_not_by_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """The application owns the id, and gets it without a round-trip.

        ADR-0003 makes a chunk's id its Qdrant point id, so the value has to be
        the application's rather than something the database chooses and hands
        back. SQLAlchemy applies the default during flush: `UserORM().id` is
        still None at construction, which is worth knowing before writing code
        that assumes otherwise.
        """
        user = _user()
        assert user.id is None, "the default is applied at flush, not __init__"

        db_session.add(user)
        await db_session.flush()

        assert isinstance(user.id, uuid.UUID)
        assert user.id.version == 4

    async def test_server_defaults_are_applied(self, db_session: AsyncSession) -> None:
        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()
        await db_session.refresh(document)

        assert user.role is UserRole.RESEARCHER
        assert user.is_active is True
        assert user.created_at is not None
        assert document.status is DocumentStatus.PENDING
        assert document.chunk_count == 0
        assert document.doc_metadata == {}
        assert document.deleted_at is None

    async def test_enum_columns_round_trip_as_values_not_names(
        self, db_session: AsyncSession
    ) -> None:
        """The database must hold 'researcher', not 'RESEARCHER'.

        The domain model, the API contract and RBACPolicy all key on the value.
        """
        user = _user(role=UserRole.ADMIN)
        db_session.add(user)
        await db_session.flush()

        stored = await db_session.execute(
            text("SELECT role FROM users WHERE id = :id"), {"id": user.id}
        )

        assert stored.scalar_one() == "admin"


def _chunk_row(
    document_id: Any, index: int = 0, content: str = "x", **extra: Any
) -> DocumentChunkORM:
    """A chunk row with the provenance M3/S3.4 made mandatory (ADR-0012 §5)."""
    fields: dict[str, Any] = {
        "document_id": document_id,
        "chunk_index": index,
        "content": content,
        "section": "1 Introduction",
        "page_start": 1,
        "page_end": 1,
        "token_count": 4,
        "strategy_version": "section-aware/v1",
        "tokenizer_id": "regex-word/v1",
    }
    fields.update(extra)
    return DocumentChunkORM(**fields)


class TestConstraints:
    async def test_email_must_be_unique(self, db_session: AsyncSession) -> None:
        """The uniqueness ADR-0003 cites as unavailable in Qdrant."""
        shared_email = f"{uuid.uuid4()}@example.test"
        db_session.add(_user(email=shared_email))
        await db_session.flush()
        db_session.add(_user(email=shared_email))

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_document_cannot_reference_a_user_that_does_not_exist(
        self, db_session: AsyncSession
    ) -> None:
        """Ownership is a foreign key, so an invented owner is rejected."""
        orphan = DocumentORM(
            user_id=uuid.uuid4(), filename="x.pdf", doc_type=DocumentType.PDF
        )
        db_session.add(orphan)

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_chunk_cannot_reference_a_document_that_does_not_exist(
        self, db_session: AsyncSession
    ) -> None:
        db_session.add(_chunk_row(uuid.uuid4()))

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_chunk_index_is_unique_within_a_document(
        self, db_session: AsyncSession
    ) -> None:
        """Makes re-ingestion idempotent and a gap in the sequence detectable."""
        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()

        db_session.add(_chunk_row(document.id, content="first"))
        await db_session.flush()
        db_session.add(_chunk_row(document.id, content="again"))

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_same_chunk_index_is_fine_in_a_different_document(
        self, db_session: AsyncSession
    ) -> None:
        """The uniqueness is scoped to the document, not global."""
        user = _user()
        db_session.add(user)
        await db_session.flush()
        first, second = _document(user), _document(user)
        db_session.add_all([first, second])
        await db_session.flush()

        db_session.add_all(
            [
                _chunk_row(first.id, content="a"),
                _chunk_row(second.id, content="b"),
            ]
        )
        await db_session.flush()

    async def test_a_negative_chunk_count_is_rejected(
        self, db_session: AsyncSession
    ) -> None:
        user = _user()
        db_session.add(user)
        await db_session.flush()
        db_session.add(_document(user, chunk_count=-1))

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_non_positive_dimension_is_rejected(
        self, db_session: AsyncSession
    ) -> None:
        """A dimension of zero is not a smaller embedding, it is a bug."""
        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()
        db_session.add(_chunk_row(document.id, dimension=0))

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_an_unknown_enum_value_is_rejected_by_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """Bypasses the ORM deliberately.

        SQLAlchemy would reject an invalid enum in Python. This asserts the
        CHECK constraint exists in PostgreSQL, so a value arriving by any other
        route — a migration, a script, psql — is refused too.
        """
        user = _user()
        db_session.add(user)
        await db_session.flush()

        with pytest.raises(DBAPIError):
            await db_session.execute(
                text("UPDATE users SET role = 'superadmin' WHERE id = :id"),
                {"id": user.id},
            )


class TestOwnershipSemantics:
    async def test_deleting_a_user_who_owns_documents_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        """RESTRICT, not CASCADE.

        Deleting a researcher must never silently destroy their corpus as a side
        effect. Deactivation is `is_active`; there is no delete path.
        """
        user = _user()
        db_session.add(user)
        await db_session.flush()
        db_session.add(_document(user))
        await db_session.flush()

        await db_session.delete(user)

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_deleting_a_document_removes_its_chunks(
        self, db_session: AsyncSession
    ) -> None:
        """CASCADE, because a chunk of a deleted document is an orphan.

        Note this is the hard-delete path. The normal path soft-deletes via
        `deleted_at` (ADR-0003), which leaves chunks in place.
        """
        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()
        db_session.add_all([_chunk_row(document.id, index=i) for i in range(3)])
        await db_session.flush()
        document_id = document.id

        await db_session.delete(document)
        await db_session.flush()

        remaining = await db_session.execute(
            select(DocumentChunkORM).where(DocumentChunkORM.document_id == document_id)
        )

        assert remaining.scalars().all() == []

    async def test_soft_deleting_a_document_keeps_the_row_and_its_chunks(
        self, db_session: AsyncSession
    ) -> None:
        """Soft delete is a column update, not a removal.

        ADR-0003 fixes the order: soft-delete in PostgreSQL, committed first,
        then delete vectors in Qdrant. Filtering reads on `deleted_at IS NULL`
        is S1.3's job; this asserts the schema supports it.
        """
        from datetime import datetime, timezone

        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()
        db_session.add(_chunk_row(document.id))
        await db_session.flush()

        document.deleted_at = datetime.now(timezone.utc)
        await db_session.flush()

        assert (await db_session.get(DocumentORM, document.id)) is not None
        chunks = await db_session.execute(
            select(DocumentChunkORM).where(DocumentChunkORM.document_id == document.id)
        )
        assert len(chunks.scalars().all()) == 1


class TestRelationships:
    async def test_a_user_reaches_their_documents(
        self, db_session: AsyncSession
    ) -> None:
        user = _user()
        db_session.add(user)
        await db_session.flush()
        db_session.add_all([_document(user), _document(user)])
        await db_session.flush()

        loaded = await db_session.execute(select(UserORM).where(UserORM.id == user.id))
        fetched = loaded.scalar_one()
        documents = await fetched.awaitable_attrs.documents

        assert len(documents) == 2

    async def test_a_chunk_reaches_its_document_and_back(
        self, db_session: AsyncSession
    ) -> None:
        user = _user()
        db_session.add(user)
        await db_session.flush()
        document = _document(user)
        db_session.add(document)
        await db_session.flush()
        chunk = _chunk_row(document.id, content="hello")
        db_session.add(chunk)
        await db_session.flush()

        parent = await chunk.awaitable_attrs.document
        children = await document.awaitable_attrs.chunks

        assert parent.id == document.id
        assert [c.id for c in children] == [chunk.id]
