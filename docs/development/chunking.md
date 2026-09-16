# Section-aware chunking

M3/S3.4 (COMPLETION_PLAN 3.6 and 3.7, ADR-005,
[ADR-0012](../adr/0012-section-aware-chunking.md)). How a `parsed` document
becomes `chunked`, and what a chunk knows about itself. The stage before it is
[pdf-extraction.md](pdf-extraction.md); the worker around both is
[ingestion.md](ingestion.md).

```text
document_pages (owner-scoped)
   │
   ▼ detect_sections            document_processing/sections.py
   │    headings by number, name, or type size
   │    sections run heading → heading; pre-heading text has no title
   ▼ SectionAwareChunker.chunk  document_processing/chunker.py
   │    token windows inside each section        (references: entry-wise)
   │    Tokenizer  ← shared/interfaces/tokenization.py
   ▼ one transaction: documents.status processing → chunked (+ chunk_count)
   │                  INSERT document_chunks
   ▼ commit
```

## The tokenizer is a seam, and since S3.5 the model fills it

ADR-005 sizes chunks with "the active embedding model's tokenizer". S3.4 was
written before that model was installed, so the chunker depends on `Tokenizer`
and never on a model:

| Member | |
|---|---|
| `id` | name and version, recorded on every chunk |
| `tokenize(text)` | every token's `[start, end)` span, in order |
| `count(text)` | how many tokens, equal to `len(tokenize(text))` |

**Spans, not `encode`/`decode`**: a chunk's content is then a verbatim substring
of the page text, with no round trip that could invent whitespace.

**The worker now passes the embedding model's own tokenizer**,
`bge-small-en-v1.5/wordpiece` (M3/S3.5, ADR-0013 §1) — the ruler that sizes a
chunk is the one that reads it. See [embeddings.md](embeddings.md).

`RegexTokenizer`, id **`regex-word/v1`**, remains in the tree: word runs
(apostrophes kept inside words), single punctuation marks, and CJK characters
one at a time, with no dependency and no model to load. It **undercounts**
against a WordPiece vocabulary — `immunohistochemistry` is one token to it and
several to the model — which was the safe direction while it was provisional,
since an undersized chunk still embeds.

**Replacing it means re-chunking, and that is now implemented.** Every chunk
records `tokenizer_id` and `strategy_version`, and the embed stage compares them
with what the pipeline currently produces: a mismatch re-chunks the document
from its stored pages before anything embeds it.

## Section detection

Signals, all deterministic, from what S3.3 stored (text, dominant font size,
page). A block is a heading when it is **one line of at most 12 words** and:

1. **numbered** — `1`, `2.3`, `4.1.2`, `IV.` followed by a title; or
2. **named** — abstract, introduction, background, related work, method(s),
   experiments, evaluation, results, discussion, limitations, conclusion,
   future work, acknowledgements, references, bibliography, appendix and the
   usual variants, optionally numbered, case-insensitive; or
3. **visibly larger** — at least 15% above the body size, where the body size is
   what most of the document's *characters* are set in. Large type alone must
   also be short and not end a sentence, so a paragraph in a big font is not
   mistaken for a heading.

Position and whitespace are **not** used: S3.3 never stored them, and adding
them for an unmeasured heuristic would change an ADR-0011 format (ADR-0012 §3).

A section runs from its heading to the next and **includes the heading block**.
Text before the first heading is a section with **no title**. A document with no
headings at all is one untitled section — which makes the fixed-window fallback
the ordinary path rather than a special case.

Sections are **flat**: one title, as it appears ("3.1 Evaluation"). Numbering
carries the hierarchy for anyone who later wants it.

## Chunking

Per section: its blocks are joined by blank lines, tokenized, and cut into
windows of `CHUNK_SIZE_TOKENS` stepping `size - overlap`.

- **Windows never cross a section boundary.**
- **Content is a verbatim substring** of that text — chunking normalises
  nothing, so a citation can be found in the document it came from.
- **Pages** come from the blocks a window actually covers: a chunk that spans a
  page break records `page_start` and `page_end` across it.
- The **last window of a section is short** rather than padded or merged.
- A section with no text produces nothing; **empty pages shift no page numbers**.

### References

In a section whose title names references or a bibliography, entries are found
at line starts as `[12]`, `(12)` or `12.`, then packed whole up to the budget.

- An entry is **never split** between chunks.
- Reference chunks carry **no overlap** — it would repeat whole citations.
- An entry longer than the budget is **windowed on its own**.
- Fewer than two entries found means the heuristic did not fire: that section
  falls back to ordinary windows. **References are never discarded.**

### Determinism

Same pages, configuration, tokenizer id and strategy version ⇒ the same chunks,
in the same order, byte for byte. No randomness, no clock, no id inside content.
Tests run the chunker twice, and chunk the same PDF under two owners and compare
what the database holds.

## What is stored

`document_chunks`, one row per chunk, ordered by `chunk_index` (unique per
document):

| Column | |
|---|---|
| `chunk_index` | 0-based, contiguous, the document's reading order |
| `content` | the chunk's text, verbatim |
| `section` | the heading it falls under; `NULL` when none was detected |
| `page_start`, `page_end` | inclusive, 1-based; equal for a chunk inside one page |
| `token_count` | counted by `tokenizer_id` |
| `strategy_version` | `section-aware/v1` |
| `tokenizer_id` | `bge-small-en-v1.5/wordpiece` in the worker; `regex-word/v1` wherever no model is loaded |

`documents.chunk_count` is written in the same statement as the status, so a
document becomes `chunked` and learns how many chunks it has together.

Reads go through `DocumentChunkRepository`, owner-scoped through the document in
SQL. **The API exposes no chunks**: a document response shows `status:
"chunked"` and nothing more. Retrieval is M4's.

## Status and the worker

`parsed → processing → chunked`. `chunked` means chunks exist and are stored;
it is **not yet searchable**. Since M3/S3.5 the same delivery carries on and
embeds them, and `ready` is committed after the vectors are persisted
(ADR-0013 §3) — `chunked` is where the embed stage starts, not where the
pipeline stops.

A delivery runs every stage the document is ready for: an upload is parsed and
then chunked by the same job, and a job that finds a `parsed` document chunks
it. Each stage is its own claim, transaction and retry, and a stage releases its
claim back to where it started — so a chunking failure retries chunking, never
parsing.

The recovery sweep covers `parsed` as well as `pending`, and since S3.5
`chunked` too: a job that dies between two stages strands a document just as
completely as one whose job never reached Redis.

| Redelivered document | What happens |
|---|---|
| `pending` | parsed, then chunked |
| `parsed` | chunked |
| `processing` | skipped — another delivery holds the claim |
| `chunked` | embedded (S3.5); the chunker is called only if the provenance is stale |
| `ready`, `failed` | skipped as terminal |
| deleted | rejected; the owner-scoped read finds nothing |

Three layers stop a document being chunked twice: the state check, the
compare-and-set claim, and `uq_document_chunks_document_id_chunk_index`.

## Failures

| Reason | When | Retried |
|---|---|---|
| `no_extracted_pages` | a `parsed` document whose pages are gone | never |
| `no_chunks_produced` | pages holding no text a chunk could be made of | never |
| `chunking_failure` | the chunker raised, or the write failed | yes, within `INGEST_MAX_TRIES` |

A failed write rolls back **both** the chunks and the status: a document is
never `chunked` without its chunks, and never holds chunks while it is not.
Logs carry counts, the strategy and the tokenizer — never chunk text.

## Configuration

| Variable | Default | |
|---|---|---|
| `CHUNK_SIZE_TOKENS` | 400 | ADR-005's "~400 tokens" |
| `CHUNK_OVERLAP_TOKENS` | 60 | its "~15% overlap" |

Settings refuses a non-positive size, a negative overlap, and an overlap at or
above the size — the chunker steps `size - overlap`, so a step of zero is a loop.
Since S3.5 these *are* the embedding model's tokens, and the worker refuses to
start unless `CHUNK_SIZE_TOKENS` plus the model's two special tokens fits what
the model reads.

## Memory

One document at a time, whole: its pages are read into memory, its sections
built as strings, and its chunks written in one transaction. For the 500-page
cap (`MAX_PDF_PAGES`) that is a few megabytes of text and a few thousand chunks.
Streaming page by page would break section boundaries, which are the point.

## Not done

- **No hierarchy**: a subsection's parent is implicit in its numbering.
- **Three-column layouts and tables** inherit S3.3's reading-order limits.
- **Reference entries** are recognised in three numbering styles; anything else
  falls back to windows.
- **No chunk API.** Nothing exposes chunk text to a client yet.
