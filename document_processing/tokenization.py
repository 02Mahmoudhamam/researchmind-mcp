"""A deterministic tokenizer for chunk sizing — M3/S3.4, provisional.

**This is not the embedding model's tokenizer, and does not pretend to be.**
ADR-0004's model is not installed and M4 chooses it; ADR-0012 §2 records the
decision to size chunks with a stand-in behind the `Tokenizer` seam until then.

What it counts: runs of word characters (with apostrophes kept inside words, so
"don't" is one token), single punctuation marks, and CJK characters
individually — the last because a WordPiece vocabulary splits those per
character too, and counting a whole Japanese sentence as one token would size
its chunks wildly wrong.

Against a WordPiece vocabulary this **undercounts**: "tokenization" is one
token here and several there. So a 400-token chunk measured by this tokenizer
is smaller than 400 of the embedding model's, which is the safe direction —
an undersized chunk still embeds, an oversized one is truncated. Every chunk
records `regex-word/v1`, so M4 can find what needs re-chunking.

No dependency, no vocabulary file, no model download in CI.
"""

import re

from shared.interfaces.tokenization import TokenSpan

# Ordered deliberately: a CJK character is matched on its own *before* `\w+`
# can swallow a whole run of them.
_TOKEN_PATTERN = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]" r"|\w+(?:['’]\w+)*" r"|[^\w\s]",
    re.UNICODE,
)

TOKENIZER_ID = "regex-word/v1"


class RegexTokenizer:
    """`Tokenizer` over a regular expression. Stateless and deterministic."""

    id = TOKENIZER_ID

    def tokenize(self, text: str) -> tuple[TokenSpan, ...]:
        return tuple(
            TokenSpan(match.start(), match.end())
            for match in _TOKEN_PATTERN.finditer(text)
        )

    def count(self, text: str) -> int:
        return sum(1 for _ in _TOKEN_PATTERN.finditer(text))
