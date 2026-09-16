"""chunk provenance and the chunked status

M3/S3.4 splits a parsed document into section-aware chunks. Two things were
missing to record the result:

* **Provenance on `document_chunks`.** ADR-0007 §1 requires `section`,
  `page_start` and `page_end`; ADR-005 §7 requires the chunking strategy to be
  recorded so a change is detectable; ADR-0012 adds the token count and the
  tokenizer those counts were made with, because M4 replaces the tokenizer and
  the boundaries depend on it. All NOT NULL but `section`, which is absent when
  no heading was detected above the chunk.
* **A status meaning "chunked, not yet searchable".** `ready` means searchable
  (ADR-0011), and chunks are not searchable until M4 embeds them, so `chunked`
  is added between `parsed` and `ready`.

Hand-written, like 0004 and 0005: autogenerate compares neither CHECK contents
nor the members of a non-native enum.

**Existing data.** Nothing has ever written a `document_chunks` row — chunk
generation is this sprint — so the columns are added to an empty table. If rows
*are* found, this migration refuses: there is no honest provenance to give a
chunk whose origin nobody recorded, and inventing one would put values into
columns later work is entitled to trust.

The downgrade drops the provenance and, with it, the chunks: without those
columns a chunk cannot say where it came from, and a later re-upgrade would
find rows it must refuse. Chunks are derived data — the chunker is
deterministic, and re-running it reproduces them exactly — so `chunked`
documents go back to `parsed` with `chunk_count` zeroed, ready to be chunked
again.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_CONSTRAINT = "ck_documents_document_status"
_STATUSES_WITH_CHUNKED = (
    "status IN ('pending', 'processing', 'parsed', 'chunked', 'ready', 'failed')"
)
_STATUSES_WITHOUT_CHUNKED = (
    "status IN ('pending', 'processing', 'parsed', 'ready', 'failed')"
)


def upgrade() -> None:
    """Add the provenance columns and allow `chunked`."""
    existing = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM document_chunks"))
        .scalar_one()
    )
    if existing:
        raise RuntimeError(
            f"document_chunks already holds {existing} row(s), written before "
            "chunk provenance existed. This migration will not invent a section, "
            "page range, token count, strategy or tokenizer for them. Delete "
            "those rows (they are derived data and can be produced again) and "
            "re-run."
        )

    op.add_column("document_chunks", sa.Column("section", sa.Text(), nullable=False))
    op.add_column(
        "document_chunks", sa.Column("page_start", sa.Integer(), nullable=False)
    )
    op.add_column(
        "document_chunks", sa.Column("page_end", sa.Integer(), nullable=False)
    )
    op.add_column(
        "document_chunks", sa.Column("token_count", sa.Integer(), nullable=False)
    )
    op.add_column(
        "document_chunks",
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "document_chunks",
        sa.Column("tokenizer_id", sa.String(length=64), nullable=False),
    )
    # A heading is not always found; the rest are always known.
    op.alter_column("document_chunks", "section", nullable=True)

    op.create_check_constraint(
        op.f("ck_document_chunks_page_start_positive"),
        "document_chunks",
        "page_start >= 1",
    )
    op.create_check_constraint(
        op.f("ck_document_chunks_page_end_not_before_start"),
        "document_chunks",
        "page_end >= page_start",
    )
    op.create_check_constraint(
        op.f("ck_document_chunks_token_count_positive"),
        "document_chunks",
        "token_count > 0",
    )

    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT), "documents", _STATUSES_WITH_CHUNKED
    )


def downgrade() -> None:
    """Drop the chunks and their provenance; `chunked` documents go back."""
    op.execute("DELETE FROM document_chunks")
    op.drop_constraint(
        op.f("ck_document_chunks_token_count_positive"),
        "document_chunks",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_document_chunks_page_end_not_before_start"),
        "document_chunks",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_document_chunks_page_start_positive"),
        "document_chunks",
        type_="check",
    )
    op.drop_column("document_chunks", "tokenizer_id")
    op.drop_column("document_chunks", "strategy_version")
    op.drop_column("document_chunks", "token_count")
    op.drop_column("document_chunks", "page_end")
    op.drop_column("document_chunks", "page_start")
    op.drop_column("document_chunks", "section")

    op.drop_constraint(op.f(_STATUS_CONSTRAINT), "documents", type_="check")
    op.execute(
        "UPDATE documents SET status = 'parsed', chunk_count = 0 "
        "WHERE status = 'chunked'"
    )
    op.execute("UPDATE documents SET chunk_count = 0 WHERE chunk_count <> 0")
    op.create_check_constraint(
        op.f(_STATUS_CONSTRAINT), "documents", _STATUSES_WITHOUT_CHUNKED
    )
