"""Text extracted from a PDF — what M3/S3.3's parsing stage produces.

The shape is what the next stage needs, and no more:

* **Pages.** ADR-0007 gives chunks `page_start` and `page_end`, and citations
  resolve to a page (principles.md §5), so page association is kept — including
  pages with no text, whose numbers would otherwise silently shift.
* **Blocks, in reading order.** ADR-005 parses in PyMuPDF's block mode with
  column-aware ordering; a block is a paragraph-sized unit of text, and its
  position in the tuple is its place in the reading order.
* **Each block's dominant font size.** ADR-005 detects section headings "by
  regex + font-size heuristics". Without the size, the chunking stage would have
  to parse the PDF again.

Nothing else — no coordinates, font names or styles. Nothing asks for them yet,
and each is recoverable by parsing the stored bytes again should it ever be.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TextBlock(BaseModel):
    """One block of text, cleaned. Never empty or whitespace-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    # The size, in points, that most of the block's characters are set in.
    font_size: float = Field(ge=0)

    @field_validator("text")
    @classmethod
    def _has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a text block must contain text")
        return value


class ExtractedPage(BaseModel):
    """One page's blocks in reading order. A page with no text has no blocks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    blocks: tuple[TextBlock, ...] = ()

    @property
    def text(self) -> str:
        """The page as plain text: its blocks in order, a blank line between."""
        return "\n\n".join(block.text for block in self.blocks)


class ExtractedText(BaseModel):
    """Every page of a document, numbered from 1 with none missing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_count: int = Field(ge=1)
    pages: tuple[ExtractedPage, ...]

    @model_validator(mode="after")
    def _every_page_once_in_order(self) -> "ExtractedText":
        numbers = [page.page_number for page in self.pages]
        if numbers != list(range(1, self.page_count + 1)):
            raise ValueError("pages must be numbered 1..page_count, each once")
        return self

    @property
    def has_text(self) -> bool:
        """Whether any page yielded any text at all."""
        return any(page.blocks for page in self.pages)
