"""Data access for a document's extracted pages — M3/S3.3.

Like chunks, a page has no owner of its own: it belongs to a document, and the
document to a user. Every method reaches ownership through the document, in the
SQL, exactly as `DocumentChunkRepository` does — a page of someone else's
document is not fetched and then refused, it is never selected.

Writes flush and never commit; the ingestion service stores a document's pages
and moves the document to `parsed` in one transaction, so a document is never
`parsed` without its pages, nor has pages while it is not.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DocumentORM, DocumentPageORM
from backend.db.repositories._identifiers import parse_id
from shared.models.extraction import ExtractedPage, TextBlock


class DocumentPageRepository:
    """Extracted pages, always reached through the owning document."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_for_user(
        self, *, document_id: str, user_id: str, pages: Sequence[ExtractedPage]
    ) -> None:
        """Store the pages of a live document this user owns.

        The document is resolved with its owner and `deleted_at IS NULL` in the
        WHERE clause first. A page already stored for the document violates the
        primary key when flushed: extraction is never recorded twice.

        :raises LookupError: the document does not exist, is not this user's,
            or is deleted — a write that cannot be attributed to an owned
            document is a defect, not an empty result.
        """
        target, owner = parse_id(document_id), parse_id(user_id)
        owned = None
        if target is not None and owner is not None:
            owned = (
                await self._session.execute(
                    select(DocumentORM.id).where(
                        DocumentORM.id == target,
                        DocumentORM.user_id == owner,
                        DocumentORM.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if owned is None:
            raise LookupError("no live document with this id belongs to this user")

        self._session.add_all(
            DocumentPageORM(
                document_id=owned,
                page_number=page.page_number,
                blocks=[block.model_dump(mode="json") for block in page.blocks],
            )
            for page in pages
        )
        await self._session.flush()

    async def list_for_document(
        self, document_id: str, user_id: str
    ) -> list[ExtractedPage]:
        """A live document's pages in order, if this user owns it; else empty."""
        target, owner = parse_id(document_id), parse_id(user_id)
        if target is None or owner is None:
            return []

        result = await self._session.execute(
            select(DocumentPageORM)
            .join(DocumentORM, DocumentPageORM.document_id == DocumentORM.id)
            .where(
                DocumentPageORM.document_id == target,
                DocumentORM.user_id == owner,
                DocumentORM.deleted_at.is_(None),
            )
            .order_by(DocumentPageORM.page_number)
        )
        return [
            ExtractedPage(
                page_number=row.page_number,
                blocks=tuple(TextBlock.model_validate(block) for block in row.blocks),
            )
            for row in result.scalars().all()
        ]
