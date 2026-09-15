"""document pages and the parsed status

M3/S3.3 extracts a PDF's text. Two things were missing to record the result:

* **Somewhere to keep the text.** `document_pages`: one row per page, keyed
  `(document_id, page_number)`, its blocks in reading order as a JSONB array of
  `{"text", "font_size"}`. The composite primary key makes it impossible to
  store the same page of a document twice. `ON DELETE CASCADE` from `documents`,
  as for chunks: extracted text is derived data.
* **A status meaning "text extracted, not yet searchable".** `ready` already
  means searchable — ADR-0009, the M3 definition of done — so `parsed` is added
  between `processing` and `ready` rather than `ready` being set early.

Hand-written, like 0004: autogenerate compares neither CHECK contents nor the
members of a non-native enum.

Existing data: untouched on upgrade; no row can already be `parsed`. Downgrade
drops every extracted page and returns `parsed` documents to `pending` — the
honest state for a sound document the old schema has no record of processing,
and the one the recovery sweep re-queues once the schema is upgraded again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-15 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_CONSTRAINT = "ck_documents_document_status"


def upgrade() -> None:
    """Allow `parsed`, then create the pages table."""
    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT),
        "documents",
        "status IN ('pending', 'processing', 'parsed', 'ready', 'failed')",
    )
    op.create_table(
        "document_pages",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "page_number >= 1", name=op.f("ck_document_pages_page_number_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blocks) = 'array'",
            name=op.f("ck_document_pages_blocks_is_array"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_pages_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "page_number", name=op.f("pk_document_pages")
        ),
    )


def downgrade() -> None:
    """Drop the pages; `parsed` documents go back to `pending`."""
    op.drop_table("document_pages")
    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.execute("UPDATE documents SET status = 'pending' WHERE status = 'parsed'")
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT),
        "documents",
        "status IN ('pending', 'processing', 'ready', 'failed')",
    )
