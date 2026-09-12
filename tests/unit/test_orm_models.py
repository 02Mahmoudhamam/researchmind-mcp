"""Schema shape, asserted without a database.

These check what the ORM *declares*. Whether PostgreSQL enforces it is
tests/integration/test_orm_persistence.py; whether a migration produces it is
tests/integration/test_migrations.py.
"""

from typing import Any

import pytest
from sqlalchemy import Enum as SAEnum

from backend.db.base import Base
from backend.db.models import DocumentChunkORM, DocumentORM, UserORM
from shared.models.document import DocumentStatus, DocumentType
from shared.models.user import UserRole


class TestTables:
    def test_exactly_the_three_approved_tables_exist(self) -> None:
        """S1.2 is authorised for User, Document and DocumentChunk only.

        Fails if a fourth entity is added without the authorisation to add it.
        """
        assert set(Base.metadata.tables) == {"users", "documents", "document_chunks"}

    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            (
                "users",
                {
                    "id",
                    "email",
                    "full_name",
                    "role",
                    "is_active",
                    "created_at",
                    "updated_at",
                },
            ),
            (
                "documents",
                {
                    "id",
                    "user_id",
                    "filename",
                    "doc_type",
                    "status",
                    "metadata",
                    "chunk_count",
                    "created_at",
                    "updated_at",
                    "deleted_at",
                },
            ),
            (
                "document_chunks",
                {
                    "id",
                    "document_id",
                    "chunk_index",
                    "content",
                    "metadata",
                    "embedding_model_id",
                    "dimension",
                    "created_at",
                },
            ),
        ],
    )
    def test_columns_are_exactly_as_designed(
        self, table: str, expected: set[str]
    ) -> None:
        """Pinned in both directions: a missing column and an extra one both fail."""
        actual = {c.name for c in Base.metadata.tables[table].columns}

        assert actual == expected


class TestAuthenticationBoundary:
    def test_no_credential_columns_anywhere(self) -> None:
        """Authentication is M2. A credential column here would pre-empt it."""
        forbidden = {"password", "password_hash", "hashed_password", "salt", "token"}

        for name, table in Base.metadata.tables.items():
            leaked = {c.name for c in table.columns} & forbidden
            assert not leaked, f"{name} carries credential columns: {leaked}"


class TestOwnership:
    def test_a_document_must_belong_to_a_user(self) -> None:
        column = Base.metadata.tables["documents"].c.user_id
        fk = next(iter(column.foreign_keys))

        assert column.nullable is False, "ownership cannot be optional"
        assert fk.column.table.name == "users"

    def test_a_chunk_must_belong_to_a_document(self) -> None:
        column = Base.metadata.tables["document_chunks"].c.document_id
        fk = next(iter(column.foreign_keys))

        assert column.nullable is False
        assert fk.column.table.name == "documents"

    def test_deleting_a_user_is_restricted_not_cascaded(self) -> None:
        """Deleting a user must never silently destroy their corpus.

        Deactivation is `users.is_active`; there is no user-delete path. RESTRICT
        makes the destructive version impossible rather than merely unwritten.
        """
        fk = next(iter(Base.metadata.tables["documents"].c.user_id.foreign_keys))

        assert fk.ondelete == "RESTRICT"

    def test_deleting_a_document_cascades_to_its_chunks(self) -> None:
        """Chunks are derived data with no independent existence."""
        fk = next(
            iter(Base.metadata.tables["document_chunks"].c.document_id.foreign_keys)
        )

        assert fk.ondelete == "CASCADE"


class TestEnums:
    @pytest.mark.parametrize(
        ("table", "column", "enum"),
        [
            ("users", "role", UserRole),
            ("documents", "doc_type", DocumentType),
            ("documents", "status", DocumentStatus),
        ],
    )
    def test_enum_columns_store_values_and_are_check_constrained(
        self, table: str, column: str, enum: Any
    ) -> None:
        """Two defaults that are both wrong for this project.

        `create_constraint` has been False since SQLAlchemy 1.4, so
        `native_enum=False` alone yields a bare VARCHAR that accepts any string.
        And without `values_callable` SQLAlchemy persists the enum *name*
        ("RESEARCHER") while the domain model, API and RBAC policy all use the
        *value* ("researcher").
        """
        type_ = Base.metadata.tables[table].c[column].type

        assert isinstance(type_, SAEnum)
        assert type_.create_constraint is True, "no CHECK: any string would be accepted"
        assert set(type_.enums) == {m.value for m in enum}


class TestDeferredColumns:
    @pytest.mark.parametrize(
        "column",
        ["storage_key", "content_hash", "size_bytes", "mime_type", "page_count"],
    )
    def test_document_storage_columns_are_not_here_yet(self, column: str) -> None:
        """ADR-0008 assigns these to Milestone M3, with the code that writes them."""
        assert column not in Base.metadata.tables["documents"].c

    @pytest.mark.parametrize("column", ["section", "page_start", "page_end"])
    def test_chunk_section_columns_are_not_here_yet(self, column: str) -> None:
        """ADR-0007 §1 assigns these to Milestone M3."""
        assert column not in Base.metadata.tables["document_chunks"].c

    def test_embedding_vectors_are_not_stored_in_postgresql(self) -> None:
        """Qdrant holds vectors; no ADR asks PostgreSQL to duplicate an index.

        The *metadata* is here, per ADR-0005, so a model change is detectable.
        """
        columns = set(Base.metadata.tables["document_chunks"].c.keys())

        assert "embedding" not in columns
        assert {"embedding_model_id", "dimension"} <= columns


class TestReservedNames:
    @pytest.mark.parametrize(
        ("model", "attribute"),
        [(DocumentORM, "doc_metadata"), (DocumentChunkORM, "chunk_metadata")],
    )
    def test_metadata_column_is_mapped_under_a_different_attribute(
        self, model: Any, attribute: str
    ) -> None:
        """`metadata` is reserved on a declarative class — it is Base.metadata.

        The column keeps the name `metadata` so the API contract is unchanged;
        only the Python attribute differs.
        """
        assert getattr(model, attribute).key == attribute
        # table.c is keyed by column name, the mapper by attribute name — this
        # asserts the mapping between the two.
        assert model.__mapper__.columns[attribute].name == "metadata"
        assert "metadata" in model.__table__.c


class TestIdentity:
    @pytest.mark.parametrize("model", [UserORM, DocumentORM, DocumentChunkORM])
    def test_primary_key_is_a_uuid_with_a_client_side_default(self, model: Any) -> None:
        """Generated in Python, not by the database.

        SQLAlchemy applies the default during flush, so the value exists before
        the INSERT is sent and needs no RETURNING round-trip to read back. That
        is what ADR-0003 needs: a chunk's id is also its Qdrant point id, so the
        application must own it rather than discover it.

        Note this is flush-time, not construction-time — `UserORM().id` is None.
        """
        pk = list(model.__table__.primary_key.columns)[0]

        assert pk.name == "id"
        assert pk.type.python_type.__name__ == "UUID"
        assert pk.default is not None, "id must be generated client-side"
