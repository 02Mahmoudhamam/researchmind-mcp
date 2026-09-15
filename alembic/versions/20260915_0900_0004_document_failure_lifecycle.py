"""document failure lifecycle: FAILED and failure_reason

ADR-0009 §4 fixes the lifecycle as `PENDING → PROCESSING → READY | FAILED`, "with
the failure reason persisted". The schema from 0001 had neither: it inherited
the scaffold's `error` status, and nowhere to say why.

* **`error` becomes `failed`.** ADR-0009 is the accepted decision and names the
  state FAILED. No code has ever written `error` — the scaffold declared it,
  migration 0001 copied it into the CHECK constraint, and nothing set it — but
  a row might exist, so the data is migrated rather than assumed away.
* **`failure_reason`**, VARCHAR(64), nullable. A stable code, never an exception
  message. Its values are not constrained in the database: new reasons arrive
  with the code that produces them, and a CHECK listing them would need a
  migration each time.
* **`ck_documents_failure_reason_iff_failed`**: `(status = 'failed') =
  (failure_reason IS NOT NULL)`. A failure is never recorded without a reason,
  and a reason never outlives the failure it explains.

Hand-written. Autogenerate does not compare CHECK constraint contents, so it
cannot see that an enum member changed.

Existing data: a row still `error` becomes `failed` with reason `unknown` — the
honest answer, since nothing recorded why at the time. Downgrade maps `failed`
back to `error` and drops the reason, which loses it; that is what reversing
this migration means.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_CONSTRAINT = "ck_documents_document_status"
_REASON_CONSTRAINT = "ck_documents_failure_reason_iff_failed"


def upgrade() -> None:
    """Rename the terminal state, then add the reason and its invariant."""
    # The old CHECK forbids 'failed', so it has to go before the data changes.
    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.add_column(
        "documents", sa.Column("failure_reason", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE documents SET status = 'failed', failure_reason = 'unknown' "
        "WHERE status = 'error'"
    )
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT),
        "documents",
        "status IN ('pending', 'processing', 'ready', 'failed')",
    )
    op.create_check_constraint(
        op.f(_REASON_CONSTRAINT),
        "documents",
        "(status = 'failed') = (failure_reason IS NOT NULL)",
    )


def downgrade() -> None:
    """Back to `error` and no reason. The reasons are lost, by definition."""
    op.drop_constraint(op.f(_REASON_CONSTRAINT), "documents", type_="check")
    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.execute("UPDATE documents SET status = 'error' WHERE status = 'failed'")
    op.drop_column("documents", "failure_reason")
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT),
        "documents",
        "status IN ('pending', 'processing', 'ready', 'error')",
    )
