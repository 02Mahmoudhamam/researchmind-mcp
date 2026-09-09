# ResearchMind MCP — Product Vision

**Author:** AI Solutions Architect review
**Date:** 2026-09-08
**Status:** Proposed — supersedes the implicit vision in `README.md`, pending owner approval
**Companions:** [ARCHITECTURE_AUDIT.md](../architecture/AUDIT-2026-09.md) · [DECISIONS.md](../adr/0000-original-decision-record.md) · [COMPLETION_PLAN.md](COMPLETION_PLAN.md)

Evidence labels: `[FACT — file:line]` directly read · `[INFERENCE]` reasoned from facts · `[ASSUMPTION]` needs owner confirmation.

---

## 1. Vision

ResearchMind is a **private research corpus you can interrogate in natural language** — and,
crucially, that your AI assistant can interrogate *on your behalf*. A researcher drops the
papers they actually care about into it; the system parses, structures and indexes them, and
then answers grounded questions across that corpus with citations back to specific passages.
The audience is the individual academic, PhD student, or small research group who has 50–500
PDFs scattered across a downloads folder and a reference manager, and who currently answers
"which of these papers used a control group?" or "where do these three disagree?" by opening
each one and reading. `[INFERENCE — from shared/models/user.py:8-11 and backend/security/rbac.py:9-19, where RESEARCHER is the central role]`

The distinctive bet is **MCP as the primary interface, not a bolted-on API**. Most RAG tools
make you come to their chat window. ResearchMind exposes its capabilities as Model Context
Protocol tools, so the corpus becomes something Claude Desktop — or any MCP host — can reach
into mid-conversation, without the researcher leaving the tool they were already working in.
`[FACT — README.md:1-12; docs/MCP.md:1-34]` The core value is therefore not "another chat-with-your-PDF
app" but **a portable, self-hosted knowledge substrate that plugs into the assistant you
already use**, with the privacy properties that matter when the documents are unpublished
manuscripts, confidential preprints, or grant applications. `[INFERENCE]`

---

## 2. Primary use case — end-to-end user journey

`[INFERENCE — reconstructed from backend/api/routers/*, shared/models/document.py:15-19, and backend/api/schemas/search.py:7-11]`

**Persona:** Dr. Ana Reyes, third-year postdoc, writing a related-work section across ~40
papers on sparse attention.

| # | Step | What the user does | What the system does | Backing code (today's state) |
|---|---|---|---|---|
| 1 | **Onboard** | Runs `docker compose up`, registers an account | Issues a JWT bound to a persisted user | `backend/api/routers/auth.py:9-18` — **stub** |
| 2 | **Ingest** | Drags 40 PDFs into the upload page | Stores each file, creates a `Document` row at `PENDING`, queues processing | `backend/api/routers/documents.py:11-18` — **stub** |
| 3 | **Process** | Watches status chips flip `PENDING → PROCESSING → READY` | Parses PDF text + metadata, splits into section-aware chunks, embeds, upserts to the vector store scoped to her user id | `document_processing/pipeline.py:24-30` — **stub**; `DocumentStatus` already models these states `[FACT — shared/models/document.py:15-19]` |
| 4 | **Ask (in-app)** | "Which of these use learned sparsity rather than fixed patterns?" | Embeds the question, retrieves top-k owner-scoped chunks, hands them to Claude as fenced context, returns a grounded answer with per-claim citations | `backend/services/search_service.py:13-21` — **stub**; retrieval→generation edge **does not exist** `[FACT — audit §2.2 Stage 7]` |
| 5 | **Ask (in Claude Desktop)** | Mid-conversation: "search my ResearchMind corpus for sparsity ablations" | Claude calls the `semantic_search` MCP tool; results flow back into her existing chat | `mcp/tools/semantic_search.py:20-23` — **stub**; schema has no `query` field `[FACT — mcp/tools/semantic_search.py:7-10]` |
| 6 | **Go deeper** | "Summarize the three closest, then tell me what none of them address" | Tool-use loop: `semantic_search` → `summarize_paper` → `detect_research_gaps` | All seven tool handlers **stubbed** `[FACT — mcp/server/server.py:47-50]` |
| 7 | **Extract** | "Give me these as BibTeX" | `extract_citations` over the intact reference sections | `mcp/tools/extract_citations.py` — **stub** |
| 8 | **Return** | Comes back next week; prior session context is still there | Session memory keyed in Redis | `memory_system/redis/store.py:10` — **stubbed and orphaned** `[FACT — audit §1.5]` |

`[INFERENCE]` **Steps 4 and 5 are the product.** Steps 6–8 are breadth. The vertical slice worth
building first is 1→2→3→4→5 for *one* tool, which is exactly what
[COMPLETION_PLAN.md](COMPLETION_PLAN.md) Phase 3 scopes.

**Dominant query shape:** `[FACT — backend/api/schemas/agents.py:6-11]` `AgentRunRequest` carries an
explicit `document_ids: List[str]`, and `[FACT — backend/api/schemas/search.py:11]` `SearchRequest`
does too. `[INFERENCE]` The system is designed around *"operate on these papers I selected"*
more than *"search everything I own"* — which argues for strong per-document filtering and
makes the missing payload index (`[FACT — audit §4.8]`) a first-class concern, not a scaling detail.

---

## 3. Elevator description (README seed)

> **ResearchMind MCP** turns a folder of research papers into a private, queryable knowledge
> base that answers questions with citations back to the exact passage. It runs entirely on
> your own machine — Postgres, a vector store, and your Anthropic key — so unpublished work
> never leaves it. Because every capability is exposed over the Model Context Protocol, your
> corpus becomes something Claude Desktop can search mid-conversation, not another chat window
> you have to visit.

`[INFERENCE]` This wording deliberately drops three claims the current README makes that the
code does not support — see §4.

---

## 4. Claimed vs. coherent

### 4.1 What the repo claims

`[FACT — README.md:3]` *"Production-grade AI Research Assistant built on the Model Context Protocol."*
`[FACT — README.md:9-12]` *"enables researchers to upload papers... then leverage a multi-agent AI
system to analyze, summarize, compare, and extract knowledge."*
`[FACT — docs/MCP.md:5-13]` Seven tools tabulated as though callable.
`[FACT — docs/AGENTS.md:17-35]` A nine-agent interaction flow diagrammed as though it runs.

### 4.2 What is actually true

`[FACT — audit §1.4]` 92 `TODO` markers, 99 bare-`...` bodies. Every module that would perform
work is a stub. `[FACT — verified]` `docker compose up` cannot build. `[FACT — verified]`
`import mcp` resolves to this repo's own directory, so the MCP server cannot import its SDK.

`[INFERENCE]` **The gap is not "incomplete" — it is a category error in the documentation.** The
docs are written in the present indicative for a system that has never run. The one honest
artefact is `[FACT — docs/ROADMAP.md:3-8]` Phase 1, where upload, summarization, semantic search
and JWT auth are all unchecked. Every other doc contradicts it.

### 4.3 Where the stated vision is internally contradictory

Four contradictions are in the repository *as design*, not as missing code. Each must be
resolved by decision, not by implementation — see [DECISIONS.md](../adr/0000-original-decision-record.md).

1. **The MCP server is deployed in a way it cannot work.**
   `[FACT — mcp/server/server.py:77-80]` The server speaks **stdio only**. `[FACT — docker-compose.yml:17-23]`
   It is deployed as a long-lived container with **no ports and nothing attached to stdin**.
   `[FACT — .env.example:14-16]` Meanwhile `MCP_SERVER_HOST`/`MCP_SERVER_PORT=8001` imply a network
   service, and `[FACT — docs/ARCHITECTURE.md:16-18]` the architecture diagram draws FastAPI calling
   the MCP server over "MCP Protocol". `[INFERENCE]` The container will start and sit inert. → **ADR-001**

2. **FastAPI calling its own code over a protocol is architectural theater.**
   `[INFERENCE]` The diagram's `FastAPI → MCP Server → Agents` edge would have the backend
   serialize a request, cross a process boundary, and land in code it could have imported. The
   audit found `[FACT — audit §1.5]` `ResearchMindMCPClient` is stubbed and never instantiated —
   the correct fix is to **delete that intent**, not to build the edge. → **ADR-001**

3. **The single-vendor story is false at the embedding layer.**
   `[FACT — .env.example:8-10]` Only `ANTHROPIC_API_KEY` is configured. `[FACT — document_processing/embedder.py:9]`
   The default embedding model is OpenAI's `text-embedding-3-small`. `[FACT — pyproject.toml]` `openai`
   is not a dependency and no `OPENAI_API_KEY` exists anywhere. `[INFERENCE]` Anthropic ships no
   embeddings endpoint, so the `"anthropic/openai"` comment at `embedder.py:11` cannot be
   satisfied as written. → **ADR-002**

4. **"Multi-agent" describes nine copies of one agent.**
   `[FACT — verified by diff]` All nine `agents/*/service.py` are byte-identical apart from one
   docstring line — same model, same temperature, same 4096 token budget, same prompt shape,
   and `[FACT — audit §1.5]` no agent imports anything outside `agents/` and `shared/`. `[INFERENCE]`
   The Router is a paid LLM call that duplicates, more slowly and expensively, the tool
   selection Claude performs natively. The word "multi-agent" is currently a description of the
   directory layout, not of any behaviour. → **ADR-003**

### 4.4 Where the vision is over-scoped

`[FACT — docs/ROADMAP.md:10-29]` Phases 2–4 commit to knowledge-graph visualization, citation
export in three styles, team workspaces, multi-tenancy, horizontal agent scaling, usage
metering and billing, SSO/SAML, on-premise deployment, and **custom model fine-tuning**.

`[INFERENCE]` This is the roadmap of a funded product, attached to a codebase where nothing
runs. Two items are actively harmful to keep on the list: *custom model fine-tuning* has no
plausible relationship to the problem (a RAG system's quality lever is retrieval, not weights),
and *usage metering and billing* presupposes a commercial context the repo gives no evidence for.
`[ASSUMPTION — needs confirmation]` I read the roadmap as aspirational framing rather than
committed scope; if any of it is genuinely committed, say so and the plan changes.

`[INFERENCE]` Two of the seven headline tools are also considerably harder than the other five
and should be honestly staged rather than listed as peers: `build_knowledge_graph` requires
entity resolution across documents, and `detect_research_gaps` requires reasoning about the
*absence* of evidence — which is exactly what retrieval-augmented systems are worst at, since
retrieval can only return what exists.

### 4.5 The corrected scope

`[INFERENCE]` The coherent product, in priority order:

| Tier | Capability | Justification |
|---|---|---|
| **Core** | Ingest PDFs → owner-scoped semantic search → grounded, cited answers, in-app and over MCP | This is the whole value proposition; steps 1–5 of §2 |
| **Core** | `summarize_paper`, `extract_citations` | Deterministic, verifiable, high daily utility; citations depend only on intact reference sections |
| **Adjacent** | `compare_papers`, `generate_research_questions` | Same machinery, more prompt design |
| **Hard** | `detect_research_gaps`, `build_knowledge_graph` | Genuinely research-grade problems; ship after evaluation exists to prove they work |
| **Out of scope (now)** | Billing, SSO/SAML, fine-tuning, multi-tenancy | No evidence of need; each would distort the architecture |

`[INFERENCE]` **The honest framing to carry into the README:** this is a self-hosted, single-tenant
research corpus with an MCP interface. "Production-grade" should mean *correct, observable,
tested and reproducible for one researcher or one lab* — not *enterprise multi-tenant SaaS*.
That is an achievable and genuinely impressive target, and it is the one the code is shaped for.

---

## 5. Success criteria

`[INFERENCE]` The vision is realised when all of the following are demonstrably true:

1. `docker compose up -d` on a clean machine with one `ANTHROPIC_API_KEY` yields a working system. `[FACT — currently impossible: audit §4.5]`
2. A researcher uploads a real PDF and asks a question, and the answer cites passages that a human can verify against the source.
3. The same question, asked from Claude Desktop over MCP, returns the same grounded result.
4. User A can never retrieve User B's content — enforced server-side and covered by a test. `[FACT — currently unenforceable: audit Chain B]`
5. A retrieval-quality change can be *measured*, not argued about. `[FACT — no evaluation exists: audit §4.4]`
6. Qdrant or the Anthropic API going down produces a clear 503, not a stack trace with internals. `[FACT — currently leaks exception strings: agents/*/service.py:37]`
7. The README describes only what actually works, in the present tense.

Criteria 1–4 are the **showable milestone** (Plan Phases 0–3). Criteria 5–7 are the
**production-grade line** (Phases 4–5).
