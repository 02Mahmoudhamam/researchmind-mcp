"""add users.password_hash

The first credential column in this schema. Migration 0001 said plainly that it
carried none, because "a credential column here would encode a decision that
milestone has not made" — M2 has now made it, and this is that decision:

* **bcrypt**, stored as its own self-describing digest (``$2b$<cost>$<salt+hash>``).
  The cost factor and salt travel inside the value, so rotating the cost later
  needs no schema change and no column to record which algorithm produced which
  row.
* **Nullable.** Deliberately, and this is the part worth reading twice. Every
  row that exists when this runs has no password, because nothing in the system
  has ever been able to set one. NOT NULL would force a value for each of them,
  and the only values available are invented ones — a credential nobody chose,
  indistinguishable at a glance from one somebody did. NULL states the truth:
  this account cannot be signed into with a password. ``verify_password``
  already returns False for it, so the safe behaviour is the default behaviour.
* **VARCHAR(255)**, where 60 would fit today's output exactly. The slack is for
  a future cost factor or algorithm; widening a column on a populated table is
  a migration nobody wants to need.

Safe on a non-empty table: ``ADD COLUMN`` with no default and no NOT NULL takes
no table rewrite and no lock beyond the catalogue update on PostgreSQL 11+. No
existing row is read, written or deleted.

S2.3 adds the column and the hashing primitives. Nothing writes to it —
registration is S2.4.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14 12:31:07.596108

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the credential column."""
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Drop it again.

    Destructive in the way every credential rollback is: the hashes go with the
    column, and bcrypt digests are not recoverable from anything else. That is
    correct behaviour for a reversal, and the reason a downgrade past this point
    in an environment that has real accounts is a decision, not a routine step.

    Nothing else in the row is touched.
    """
    op.drop_column("users", "password_hash")
