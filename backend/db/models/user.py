"""Persistence model for a user.

Identity, ownership, and — as of Sprint M2/S2.3 — a credential. `password_hash`
is the only thing here that is not identity, and it is a bcrypt digest: the
plaintext exists in this process for the length of one function call in
`backend/security/passwords.py` and is never stored, logged or returned.

The Pydantic `User` in `shared/models/user.py` deliberately does **not** mirror
it. That model is the API contract, and the ORM/domain split exists precisely
so that a column can be storage-only.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from shared.models.user import UserRole

if TYPE_CHECKING:
    from backend.db.models.document import DocumentORM


class UserORM(Base):
    """A researcher who owns documents.

    Mirrors ``shared.models.user.User`` field-for-field. The two are kept
    separate on purpose: the Pydantic model is the API contract, this is
    storage, and mapping between them belongs to the repositories in S1.3.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Unique because it is the login identifier M2 will authenticate against,
    # and because uniqueness on email is one of the guarantees ADR-0003 cites
    # as unavailable in Qdrant. Enforced by the database, not by application
    # convention.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # native_enum=False stores the value as VARCHAR with a CHECK constraint
    # rather than a PostgreSQL ENUM type. A native enum needs ALTER TYPE to
    # gain a member and cannot drop one at all, which makes a reversible
    # migration awkward; a CHECK is rewritten by an ordinary ALTER TABLE.
    #
    # values_callable is not optional: without it SQLAlchemy persists the enum
    # *name* ("RESEARCHER"), while the domain model, the API and the RBAC
    # policy all use the *value* ("researcher").
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            # Not the default. Since SQLAlchemy 1.4 `create_constraint` is False,
            # so `native_enum=False` alone yields a bare VARCHAR and the database
            # accepts any string at all — the enum would be enforced only in
            # Python, which is precisely the "convention rather than constraint"
            # ADR-0003 rejects Qdrant for.
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=UserRole.RESEARCHER,
    )

    # Deactivation is how a user is removed from service. There is no user
    # delete: see the RESTRICT foreign key on documents.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # A bcrypt digest, or NULL for an account that has no password yet.
    #
    # Nullable, and that is a decision rather than an oversight. S2.3 adds the
    # column; S2.4 adds registration, which is the only thing that will ever
    # write it. Making it NOT NULL would require inventing a credential for
    # every existing row — a password nobody chose, which is worse than no
    # password at all because it looks like one. NULL says "this account cannot
    # be signed into with a password", which is exactly true, and
    # `verify_password` already returns False for it.
    #
    # String(60) would fit today's `$2b$12$…` exactly. 255 is deliberate slack:
    # a future cost factor, a different bcrypt variant prefix, or a migration to
    # another algorithm all change the length, and widening a column on a
    # populated table is a migration nobody wants to need. The value is opaque
    # to the database either way.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Not present on the Pydantic model, and added deliberately: `role` and
    # `is_active` are both mutable, so a row can change with no record of when.
    # This is persistence metadata rather than an API field, which is exactly
    # the kind of thing the ORM/domain split exists to allow.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    documents: Mapped[list["DocumentORM"]] = relationship(
        back_populates="owner",
        # No cascade. Deleting a user must not delete their corpus — the
        # foreign key is RESTRICT and this mirrors it.
        passive_deletes="all",
    )

    def __repr__(self) -> str:
        return f"<UserORM id={self.id} email={self.email!r}>"
