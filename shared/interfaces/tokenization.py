"""Counting tokens — the seam between chunking and whatever tokenizes text.

ADR-005 sizes chunks in tokens "counted with the active embedding model's
tokenizer". That model is ADR-0004's, it is not installed, and M4 chooses it.
So the chunker depends on this protocol and never on a model (ADR-0012 §2).

The protocol is the minimum the chunker uses. In particular it exposes token
**spans** rather than `encode`/`decode`: a chunk's content is then a verbatim
substring of the text it came from, with no round trip through an encoder that
could invent whitespace or drift from what was stored.

An implementation must be deterministic — the same text always yields the same
spans — and must identify itself, because token counts, and therefore chunk
boundaries, mean nothing without knowing what counted them.
"""

from typing import NamedTuple, Protocol


class TokenSpan(NamedTuple):
    """Half-open `[start, end)` character offsets of one token in its text."""

    start: int
    end: int


class Tokenizer(Protocol):
    """Splits text into countable tokens, and says which tokenizer it is."""

    # Name and version, e.g. "regex-word/v1". Stored on every chunk it sizes,
    # so the chunks a later tokenizer invalidates are a query, not a guess.
    id: str

    def tokenize(self, text: str) -> tuple[TokenSpan, ...]:
        """Every token's span, in order, non-overlapping."""
        ...

    def count(self, text: str) -> int:
        """How many tokens `text` holds. Equal to `len(tokenize(text))`."""
        ...
