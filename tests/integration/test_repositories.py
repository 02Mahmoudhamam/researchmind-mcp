"""Repository behaviour against real PostgreSQL.

Cross-user isolation has its own file — tests/integration/test_repository_security.py
— because it is a security boundary rather than ordinary CRUD, and mixing the
two makes it easy to weaken one while reading the other.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.repositories import (
    DocumentChunkRepository,
    DocumentRepository,
    UserRepository,
)
from shared.models.chunking import Chunk, ChunkingResult
from shared.models.document import (
    DocumentMetadata,
    DocumentStatus,
    DocumentType,
)
from shared.models.user import User, UserRole

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]


def _email() -> str:
    """A unique, *valid* address.

    Not `.test`: it is an IANA special-use TLD and `EmailStr` rejects it. The
    `users.email` column is a plain VARCHAR with no such opinion, so a row
    written by a script or a migration can be perfectly legal in PostgreSQL and
    still fail validation the moment a repository maps it to the domain model.
    Worth knowing before writing seed data.
    """
    return f"{uuid.uuid4()}@example.com"


async def _make_user(session: AsyncSession, **kwargs: object) -> User:
    return await UserRepository(session).create(
        email=str(kwargs.get("email", _email())),
        full_name=str(kwargs.get("full_name", "Ada Lovelace")),
    )


def _chunking(*chunks: Chunk) -> ChunkingResult:
    """What the chunker hands the repository (M3/S3.4)."""
    return ChunkingResult(
        chunks=chunks, strategy_version="section-aware/v1", tokenizer_id="regex-word/v1"
    )


def _chunk(index: int, content: str = "text") -> Chunk:
    """A chunk as M3/S3.4's chunker produces one, with its provenance."""
    return Chunk(
        chunk_index=index,
        content=content,
        section="1 Introduction",
        page_start=1,
        page_end=1,
        token_count=len(content.split()) or 1,
    )


class TestUserRepository:
    async def test_create_returns_a_domain_model_not_an_orm_row(
        self, db_session: AsyncSession
    ) -> None:
        """No ORM object escapes the repository.

        A caller holding a detached ORM instance hits lazy-load errors far from
        the cause; a Pydantic model has no such surprises.
        """
        user = await _make_user(db_session)

        assert isinstance(user, User)
        assert not hasattr(user, "_sa_instance_state")
        assert uuid.UUID(user.id).version == 4
        assert user.role is UserRole.RESEARCHER
        assert user.is_active is True

    async def test_get_by_id_round_trips(self, db_session: AsyncSession) -> None:
        created = await _make_user(db_session)

        found = await UserRepository(db_session).get_by_id(created.id)

        assert found is not None
        assert found.id == created.id
        assert found.email == created.email

    async def test_get_by_email_round_trips(self, db_session: AsyncSession) -> None:
        """The lookup M2's login will use."""
        created = await _make_user(db_session)

        found = await UserRepository(db_session).get_by_email(created.email)

        assert found is not None and found.id == created.id

    async def test_missing_user_is_none_not_an_error(
        self, db_session: AsyncSession
    ) -> None:
        repo = UserRepository(db_session)

        assert await repo.get_by_id(str(uuid.uuid4())) is None
        assert await repo.get_by_email("nobody@example.com") is None

    async def test_a_malformed_id_is_not_found_rather_than_a_crash(
        self, db_session: AsyncSession
    ) -> None:
        """Identifiers arrive from request paths and MCP arguments.

        A malformed one is untrusted input, not a programming error — raising
        would turn a crafted request into a 500.
        """
        assert await UserRepository(db_session).get_by_id("../../etc/passwd") is None

    async def test_duplicate_email_is_refused_by_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """No pre-check in the repository: check-then-insert is a race."""
        email = _email()
        await _make_user(db_session, email=email)

        with pytest.raises(IntegrityError):
            await _make_user(db_session, email=email)


class TestDocumentRepository:
    async def test_create_then_read_back(self, db_session: AsyncSession) -> None:
        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)

        created = await repo.create(
            user_id=user.id,
            filename="paper.pdf",
            doc_type=DocumentType.PDF,
            metadata=DocumentMetadata(title="On Computable Numbers", year=1936),
        )
        found = await repo.get_for_user(created.id, user.id)

        assert found is not None
        assert found.filename == "paper.pdf"
        assert found.status is DocumentStatus.PENDING
        assert found.chunk_count == 0
        assert found.metadata.title == "On Computable Numbers"
        assert found.metadata.year == 1936

    async def test_metadata_survives_the_jsonb_round_trip(
        self, db_session: AsyncSession
    ) -> None:
        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)
        metadata = DocumentMetadata(
            title="t",
            authors=["A", "B"],
            keywords=["x"],
            doi="10.1000/xyz",
            extra={"source": "arxiv"},
        )

        created = await repo.create(
            user_id=user.id,
            filename="f.pdf",
            doc_type=DocumentType.PDF,
            metadata=metadata,
        )
        found = await repo.get_for_user(created.id, user.id)

        assert found is not None
        assert found.metadata == metadata

    async def test_list_returns_newest_first(self, db_session: AsyncSession) -> None:
        """Ordering matches ix_documents_user_id_created_at."""
        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)
        for name in ("first.pdf", "second.pdf", "third.pdf"):
            await repo.create(user_id=user.id, filename=name, doc_type=DocumentType.PDF)

        listed = await repo.list_for_user(user.id)

        assert [d.filename for d in listed] == ["third.pdf", "second.pdf", "first.pdf"]

    async def test_a_user_with_no_documents_gets_an_empty_list(
        self, db_session: AsyncSession
    ) -> None:
        user = await _make_user(db_session)

        assert await DocumentRepository(db_session).list_for_user(user.id) == []

    async def test_creating_for_an_unknown_user_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        """The foreign key refuses an invented owner."""
        with pytest.raises(IntegrityError):
            await DocumentRepository(db_session).create(
                user_id=str(uuid.uuid4()),
                filename="x.pdf",
                doc_type=DocumentType.PDF,
            )

    async def test_creating_with_a_malformed_user_id_raises(
        self, db_session: AsyncSession
    ) -> None:
        """Unlike a read, this is a programming error rather than path input."""
        with pytest.raises(ValueError):
            await DocumentRepository(db_session).create(
                user_id="not-a-uuid", filename="x.pdf", doc_type=DocumentType.PDF
            )


class TestSoftDelete:
    async def test_delete_marks_and_hides_the_document(
        self, db_session: AsyncSession
    ) -> None:
        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)
        document = await repo.create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )

        deleted = await repo.soft_delete_for_user(document.id, user.id)

        assert deleted is True
        assert await repo.get_for_user(document.id, user.id) is None
        assert await repo.list_for_user(user.id) == []

    async def test_the_row_survives_a_soft_delete(
        self, db_session: AsyncSession
    ) -> None:
        """Soft, not hard. ADR-0003 needs the row so Qdrant can be reconciled."""
        from sqlalchemy import text

        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)
        document = await repo.create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        await repo.soft_delete_for_user(document.id, user.id)

        row = await db_session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(document.id)},
        )

        assert row.scalar_one() is not None, "deleted_at was not stamped"

    async def test_deleting_twice_reports_false_the_second_time(
        self, db_session: AsyncSession
    ) -> None:
        """Idempotent by consequence: the second UPDATE matches zero rows.

        Indistinguishable from "never existed" and from "belongs to someone
        else" — deliberately, so the return value is not an existence oracle.
        """
        user = await _make_user(db_session)
        repo = DocumentRepository(db_session)
        document = await repo.create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )

        assert await repo.soft_delete_for_user(document.id, user.id) is True
        assert await repo.soft_delete_for_user(document.id, user.id) is False

    async def test_deleting_something_that_never_existed_is_false(
        self, db_session: AsyncSession
    ) -> None:
        user = await _make_user(db_session)

        result = await DocumentRepository(db_session).soft_delete_for_user(
            str(uuid.uuid4()), user.id
        )

        assert result is False

    async def test_chunks_of_a_soft_deleted_document_become_unreachable(
        self, db_session: AsyncSession
    ) -> None:
        """The rows survive; the ownership-scoped path stops returning them."""
        user = await _make_user(db_session)
        documents = DocumentRepository(db_session)
        chunks = DocumentChunkRepository(db_session)
        document = await documents.create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        stored = await chunks.add_many(
            document_id=document.id,
            user_id=user.id,
            chunking=_chunking(_chunk(0)),
        )

        await documents.soft_delete_for_user(document.id, user.id)

        assert await chunks.get_for_user(stored[0].id, user.id) is None
        assert await chunks.list_for_document(document.id, user.id) == []


class TestDocumentChunkRepository:
    async def test_add_and_list_in_index_order(self, db_session: AsyncSession) -> None:
        user = await _make_user(db_session)
        document = await DocumentRepository(db_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        repo = DocumentChunkRepository(db_session)

        await repo.add_many(
            document_id=document.id,
            user_id=user.id,
            chunking=_chunking(
                _chunk(2, "third"), _chunk(0, "first"), _chunk(1, "second")
            ),
        )
        listed = await repo.list_for_document(document.id, user.id)

        assert [c.content for c in listed] == ["first", "second", "third"]
        assert [c.chunk_index for c in listed] == [0, 1, 2]

    async def test_a_stored_chunk_is_reachable_by_id_with_its_owner(
        self, db_session: AsyncSession
    ) -> None:
        user = await _make_user(db_session)
        document = await DocumentRepository(db_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        repo = DocumentChunkRepository(db_session)
        stored = await repo.add_many(
            document_id=document.id,
            user_id=user.id,
            chunking=_chunking(_chunk(0, "hello")),
        )

        found = await repo.get_for_user(stored[0].id, user.id)

        assert found is not None
        assert found.content == "hello"
        assert found.document_id == document.id

    async def test_the_embedding_vector_is_never_returned(
        self, db_session: AsyncSession
    ) -> None:
        """Qdrant holds vectors; PostgreSQL does not duplicate an index."""
        user = await _make_user(db_session)
        document = await DocumentRepository(db_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        repo = DocumentChunkRepository(db_session)
        stored = await repo.add_many(
            document_id=document.id,
            user_id=user.id,
            chunking=_chunking(_chunk(0)),
        )

        assert stored[0].embedding is None

    async def test_duplicate_chunk_index_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        """The S1.2 constraint that makes re-ingestion idempotent."""
        user = await _make_user(db_session)
        document = await DocumentRepository(db_session).create(
            user_id=user.id, filename="d.pdf", doc_type=DocumentType.PDF
        )
        repo = DocumentChunkRepository(db_session)

        with pytest.raises(IntegrityError):
            await repo.add_many(
                document_id=document.id,
                user_id=user.id,
                chunking=_chunking(_chunk(0, "a"), _chunk(0, "b")),
            )

    async def test_adding_to_a_document_that_does_not_exist_raises(
        self, db_session: AsyncSession
    ) -> None:
        """A write that cannot be attributed to an owned document is a bug.

        Reads collapse to None; this does not, because returning an empty list
        would report success while writing nothing.
        """
        user = await _make_user(db_session)

        with pytest.raises(LookupError):
            await DocumentChunkRepository(db_session).add_many(
                document_id=str(uuid.uuid4()),
                user_id=user.id,
                chunking=_chunking(_chunk(0)),
            )
