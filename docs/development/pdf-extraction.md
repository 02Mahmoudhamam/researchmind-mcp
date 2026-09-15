# PDF text extraction

M3/S3.3 (COMPLETION_PLAN 3.5, ADR-005, [ADR-0011](../adr/0011-pdf-text-extraction-stage.md)).
How the ingestion worker turns a verified PDF into stored text, and what it does
not do yet. The worker around it — claims, retries, the reaper and the recovery
sweep — is [ingestion.md](ingestion.md).

```text
IngestionService._process_claimed        (claim held; bytes read through Storage)
   │
   ├─ SHA-256 == recorded hash?     no → failed: content_hash_mismatch
   │
   ▼ PdfTextExtractor.extract(bytes, max_pages)          shared/interfaces/pdf_extraction.py
   │    PyMuPDFTextExtractor                              document_processing/pdf_parser.py
   │      anyio.fail_after(INGEST_PARSE_TIMEOUT_SECONDS)
   │        anyio.to_process.run_sync(extract_pdf_text, cancellable=True)
   │
   ├─ page count ≠ upload's?        → failed: page_count_mismatch
   ├─ no text on any page?          → failed: no_extractable_text
   │
   ▼ one transaction: documents.status processing → parsed
   │                  INSERT document_pages (one row per page)
   ▼ commit
```

## The seam

| Layer | Knows | Does not know |
|---|---|---|
| `IngestionService` | what a result means for a document | PyMuPDF, processes, pages' layout |
| `PdfTextExtractor` (protocol) | `extract(data: bytes, *, max_pages) -> ExtractedText` | — |
| `PyMuPDFTextExtractor` | processes, the deadline | the database, storage, owners, ARQ |
| `extract_pdf_text` | PyMuPDF, reading order, cleaning | everything else |

The parser is handed **bytes the worker has already verified** and a page
limit. Not a path, a filename, an owner, a request or a `Principal` — so it has
no way to reach storage, the database or an identity. Architecture tests pin the
parser module's imports to an explicit list, forbid it from opening files, and
check that only the worker's startup constructs it.

## What is stored

`document_pages`, one row per page:

| Column | |
|---|---|
| `document_id` | FK → `documents.id`, `ON DELETE CASCADE`; part of the primary key |
| `page_number` | from 1; part of the primary key; CHECK ≥ 1 |
| `blocks` | JSONB array, CHECK it is an array: `[{"text": "...", "font_size": 10.0}, ...]` in reading order |
| `created_at` | |

- **Every page is a row**, including pages with no text (`blocks = []`), so page
  numbers are never inferred and never shift.
- **A block** is PyMuPDF's paragraph-sized unit. Its lines are kept, joined by
  `\n`; `ExtractedPage.text` joins a page's blocks with a blank line.
- **`font_size`** is the size most of the block's characters are set in —
  what ADR-005's heading detection needs, so chunking never parses again.
- **Nothing else** is kept: no coordinates, font names or styles. Nothing asks
  for them, and the stored bytes can be parsed again should anything ever.

Read it with `DocumentPageRepository.list_for_document(document_id, user_id)` —
owner and `deleted_at IS NULL` in the SQL, like every other owned read.

**The API does not return extracted text.** A document's response shows
`status: "parsed"` and nothing more.

## Status

`pending → processing → parsed`. `parsed` means text extracted and stored, and
nothing more: not chunked, not embedded, not searchable. `ready` still means
searchable, and nothing sets it yet. ADR-0011 explains why a status and not
`ready`.

## Reading order

Measured on a generated two-column page whose right column was written first:

| Order | Result |
|---|---|
| PyMuPDF content-stream order | RIGHT-A, RIGHT-B, LEFT-A, LEFT-B — whatever the producer wrote first |
| `get_text(..., sort=True)` | LEFT-A, RIGHT-A, LEFT-B, RIGHT-B — interleaved |
| `reading_order` | LEFT-A, LEFT-B, RIGHT-A, RIGHT-B |

The rule: walking down the page, a block that **crosses the middle** (with 2%
of the page width as slack) is read where it stands — a title, an abstract, a
full-width figure, any single-column paragraph. Between two such blocks, all
left-column blocks top to bottom, then all right-column blocks. A single-column
page comes out top to bottom.

Known limits: three or more columns are read as two; a table whose cells sit
either side of the middle is read column by column. M7's evaluation is where
their cost becomes visible.

## Cleaning

Every line is NFC-normalised, and every run of whitespace — spaces, tabs,
no-break spaces, stray breaks — becomes one space. Removed entirely:

- control characters, including NUL — which PostgreSQL cannot store;
- format characters, including bidirectional overrides and zero-width
  characters — text that renders unlike what it says has no place in a prompt;
- surrogates, private-use and unassigned code points;
- U+FFFD, which is what an unmappable glyph becomes.

PyMuPDF flags: ligatures expanded (`ﬁ` → `fi`), image blocks not requested,
text outside the page's media box dropped.

## Failures

| Reason | When | Retried |
|---|---|---|
| `pdf_open_failed` | the bytes do not open as a PDF, or it has no pages | never |
| `pdf_encrypted` | a password is needed to read it (owner-restricted PDFs are read) | never |
| `page_limit_exceeded` | more than `MAX_PDF_PAGES` — by the recorded count before reading storage, or by the parser before reading any page | never |
| `pdf_parse_failed` | it opened, but a page could not be read | never |
| `pdf_timeout` | extraction outlived `INGEST_PARSE_TIMEOUT_SECONDS`; the process was killed | never |
| `parser_unavailable` | the extraction process died or would not start | yes, within `INGEST_MAX_TRIES` |
| `page_count_mismatch` | extraction counted different pages than upload did | never |
| `no_extractable_text` | no text on any page — typically a scan; there is no OCR | never |

Each is persisted as `failure_reason`. PyMuPDF's own exception messages are
discarded — they can quote the document — and `PdfExtractionError`'s message is
only its code. Anything else an extractor raises is retried as
`processing_failure`.

Upload already refuses encrypted, malformed and over-limit PDFs. The worker
refuses them again because stored bytes and rows can predate a rule, and a cap
can be lowered after upload.

## Time

`PyMuPDFTextExtractor` runs `extract_pdf_text` in a worker **process**
(`anyio.to_process.run_sync`, `cancellable=True`) inside
`anyio.fail_after(INGEST_PARSE_TIMEOUT_SECONDS)`, default 180 s:

- **Past the budget the process receives SIGKILL**, and the document fails as
  `pdf_timeout`. A thread could not be stopped, which is why ADR-0011 departs
  from ADR-0009 §3's `to_thread`.
- **The budget is below the job timeout** (`INGEST_JOB_TIMEOUT_SECONDS`, 300),
  and Settings refuses anything else: otherwise ARQ would cancel the job
  first, the claim would go back to `pending` with no reason, and the recovery
  sweep would queue the same PDF again and again.
- **A cancellation from outside** — the job's timeout, the worker stopping —
  also kills the process, and stays a cancellation (the claim is released).
- **No job waits for a process**: concurrency is capped at `ARQ_MAX_JOBS`, the
  worker's own job concurrency, so waiting never spends a document's budget.
- **The event loop is never blocked**; a test counts its ticks during a parse.

Processes are pooled per event loop by anyio and reused; the PDF's bytes cross
by pipe.

## Not done

- **No memory limit** on the extraction process. A PDF that exhausts memory
  kills its process (`parser_unavailable`, retried, then failed) — or, under an
  OOM killer that picks the worker, the reaper fails the claim.
- **No OCR**, so scans fail as `no_extractable_text`.
- **No dehyphenation**: a word broken across lines stays broken.
- **No section detection, chunking or embeddings** — the next stages, which
  start from `document_pages`.
- **PDF metadata** (title, author) is not read. `documents.metadata` is
  untouched.
- **No extraction version is recorded.** If the ordering or cleaning rules
  change, nothing marks which documents were parsed under the old ones.
