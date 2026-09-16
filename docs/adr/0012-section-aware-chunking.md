# ADR-0012 — Section-aware chunking: a `chunked` stage, a tokenizer seam, and chunk provenance

- **Status:** Accepted — §1 and §2 approved by the owner before implementation (M3/S3.4); §3–§8 are implementation decisions recorded here
- **Date:** 2026-09-16
- **Deciders:** Owner (§1, §2); implementation of M3/S3.4 for the rest
- **Related:** ADR-005 (in ADR-0000), ADR-0004, ADR-0007, ADR-0009, ADR-0011; Milestone M3/S3.4

## Context

M3/S3.3 leaves a document `parsed`: its text stored page by page, each page a
list of blocks with a dominant font size. ADR-005 requires the next stage to
detect section headings "by regex + font-size heuristics", chunk **within**
section boundaries at "~400 tokens with ~15% overlap", split references on entry
boundaries, and fall back to fixed windows when no sections are found. ADR-0007
§1 requires chunks to carry `section`, `page_start`, `page_end` and ordering.

Four things were undecided.

1. **What a chunked document is.** ADR-0011 established that `ready` means
   searchable, which needs embeddings (M4). A document with chunks and no
   vectors is not searchable.
2. **How to count tokens.** ADR-005 says the count must use "the active
   embedding model's tokenizer (ADR-002)". That model — ADR-0004's FastEmbed
   `bge-small-en-v1.5` — is not installed, is not S3.4's to install, and may
   still change.
3. **Where chunk provenance lives.** `document_chunks` has `chunk_index`,
   `content`, `metadata`, and the embedding columns ADR-005 §7 asked for. It has
   no section or page columns, and nothing has ever written a row to it.
4. **What a "section" is** in a repository that nowhere defines section
   hierarchy.

## Decision

### 1. A `chunked` status (owner-approved)

`pending → processing → parsed → processing → chunked → … → ready`. `chunked`
means section-aware chunks exist and are stored; it is not searchable. M4 owns
`ready`. Chunking never sets `ready`.

### 2. A `Tokenizer` seam, with a provisional implementation (owner-approved)

The chunker depends on a `Tokenizer` protocol, never on an embedding model. The
protocol is the minimum the chunker uses: an `id`, `tokenize(text)` returning
token **spans**, and `count(text)`.

Spans rather than `encode`/`decode`, because a chunk's content is then a
**verbatim substring** of the page text S3.3 stored — no re-joining, no
whitespace invented, nothing to drift between encode and decode.

`RegexTokenizer`, id **`regex-word/v1`**, is the implementation used now:
Unicode word runs and single punctuation marks, deterministic, no dependency, no
model download in CI. It is **provisional and deliberately not the embedding
tokenizer**. It undercounts against a WordPiece vocabulary, so 400 of its tokens
are fewer than 400 of `bge-small`'s — conservative in the direction that
matters, since a chunk that is too small still embeds.

Every chunk records the tokenizer id **and** the chunking strategy version, so
the day M4 fixes the model, the chunks needing a re-run are a query rather than
a guess.

### 3. Section detection — deterministic, three signals

A block is a heading when it is **short** (at most 12 words) and any of:

1. **Numbered:** `1`, `2.3`, `IV.` followed by a title, or
2. **A known section name:** abstract, introduction, background, related work,
   method(s|ology), materials, experiments, evaluation, results, discussion,
   limitations, conclusion(s), future work, acknowledg(e)ments, references,
   bibliography, appendix — optionally numbered, or
3. **Visibly larger type:** a dominant font size at least 15% above the
   document's body size, where the body size is the size most characters are set
   in.

Signals ADR-005 lists that are **not** used: nothing else is available. Position
and whitespace were not persisted by S3.3, and adding them now would change an
ADR-0011 format for a heuristic that has no evidence behind it yet.

A section runs from its heading to the next heading. Text before the first
heading belongs to a section with **no title** (`NULL`), not to an invented one.

### 4. Sections are flat

One `section` column holding the heading text as it appears ("3.1 Evaluation").
No hierarchy: nothing in the repository consumes one, ADR-0007's parent lookup
is by chunk neighbourhood, and the numbering is in the title for anyone who
wants it later.

### 5. Chunk provenance, in columns (migration 0006)

`section` (nullable — not every chunk has one), `page_start`, `page_end`,
`token_count`, `strategy_version`, `tokenizer_id`, all `NOT NULL` except
`section`. Ordering stays `chunk_index`, unique per document.

The columns are `NOT NULL` because every chunk is derived from pages and there
is no honest value for "unknown". Nothing has ever written a chunk row, so the
migration adds them to an empty table; if it finds rows, it **refuses and says
so** rather than inventing provenance for them.

### 6. Chunking — token windows inside a section

Per section: concatenate its blocks (keeping a page map), tokenize, and slide a
window of `CHUNK_SIZE_TOKENS` with a step of `size - overlap`. A chunk's content
is the substring spanned by its tokens; its `page_start`/`page_end` come from
the blocks it covers. **Windows never cross a section boundary.**

`CHUNK_SIZE_TOKENS = 400`, `CHUNK_OVERLAP_TOKENS = 60` — ADR-005's "~400 tokens
with ~15% overlap", not invented here. Settings refuses `size <= 0`,
`overlap < 0` and `overlap >= size`.

### 7. References split on entry boundaries

In a section whose title names references or a bibliography, entries are found
at line starts matching `[12]`, `12.`, or `(12)`. Entries are packed into chunks
up to the token budget and **never split mid-entry**; an entry longer than the
budget is windowed on its own. Reference chunks carry **no overlap** — an
overlap would repeat whole citations into neighbouring chunks.

Fewer than two entries found means the heuristic did not fire: that section
falls back to ordinary windows. References are never discarded.

### 8. Determinism is a property, not an aspiration

Same pages, configuration, tokenizer id and strategy version ⇒ the same chunks,
byte for byte, in the same order. No randomness, no clock, no id inside content.
`section-aware/v1` is the strategy version.

## Alternatives Considered

| Option | Description | Why not chosen |
|---|---|---|
| `ready` after chunking | Skip a status | Chunks are not searchable; ADR-0011 fixed `ready` as searchable, and a polling client would search nothing. |
| Install the embedding tokenizer now | Count with `bge-small`'s vocabulary | Installs M4's model in S3.4, adds a CI model download, and fixes chunk sizes to a model that may still change. |
| `tiktoken` | A ready-made tokenizer | OpenAI's vocabulary in an Anthropic-only project, and no closer to ADR-0004's model than a regex is. |
| Character counts | Avoid tokenizing | The ambiguity ADR-005 exists to end; a 4× error in effective chunk size. |
| `encode`/`decode` on the seam | Conventional tokenizer API | Round-tripping invents whitespace and can drift from the stored text; spans keep content verbatim. |
| Section hierarchy (path, level) | Model subsections | Nothing consumes it; the numbering already carries it in the title. |
| Provenance in the `metadata` JSONB | No migration | Unqueryable for the re-index ADR-005 §7 wants, and untyped for no gain. |
| Nullable provenance | Avoid the migration guard | Provenance that "should" be there is provenance no query can rely on. |
| Delete-and-replace chunks each run | Idempotency by rewriting | Nothing asks for re-chunking yet; the claim and the unique index already make one run per document. Re-chunking is M4's to design, with the version columns to drive it. |
| An ML or LLM heading classifier | Better detection | Non-deterministic, unmeasurable before M7, and explicitly out of scope. |

## Consequences

**Good.** Chunk boundaries follow the document's own structure, and every chunk
says which document, section and pages it came from, how many tokens it holds,
and which strategy and tokenizer produced it. Re-chunking after M4 fixes the
model is a query. The chunker is pure and testable: pages in, chunks out, no
database, no queue, no clock.

**Bad.** The token counts are provisional, so M4 will re-chunk everything —
paid once, and detectable. The heading heuristics will miss unusual layouts;
the fallback keeps that a degradation rather than a corruption. Reference-entry
detection recognises three numbering styles and falls back otherwise. Sections
are flat, so a subsection's parent is only implicit in its numbering.

**Neutral.** `document_chunks` gains six columns before anything embeds them.
The chunk table is written by exactly one path, and re-chunking is not
implemented.

## References

- ADR-005 (ADR-0000) §1–§4, §6, §7 — block mode, section-bounded chunking, ~400/15%, references, configuration, strategy version
- ADR-0007 §1 — `section`, `page_start`, `page_end`, ordering
- ADR-0011 — `parsed`, per-page storage, and `ready` meaning searchable
- ADR-0004 — the embedding model S3.4 must not depend on
- `docs/development/chunking.md` — the implementation
