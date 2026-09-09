> [!WARNING]
> **TARGET STATE — NOT CURRENT BEHAVIOUR.**
> This document describes the system as intended, not as implemented. As of the
> repository baseline, the described behaviour does not execute. See
> [AUDIT-2026-09.md](AUDIT-2026-09.md) for the verified current state and
> [../roadmap/MILESTONES.md](../roadmap/MILESTONES.md) for the delivery plan.

# Agent Reference

## Agent Registry

| Agent | Role | Key Tools |
|-------|------|-----------|
| Orchestrator | Coordinates all agents, decomposes complex tasks | All |
| Router | Classifies intent and routes to specialized agents | None |
| Summarizer | Generates structured summaries | summarize_paper |
| Citation | Extracts and formats citations | extract_citations |
| SemanticSearch | Finds similar content via vector search | semantic_search |
| ResearchGap | Identifies gaps in the literature | detect_research_gaps |
| KnowledgeGraph | Builds entity-relationship graphs | build_knowledge_graph |
| PaperComparison | Side-by-side paper analysis | compare_papers |
| Memory | Maintains session context across turns | Redis store |

## Interaction Flow

```
User Message
     │
     ▼
Orchestrator (decomposes task)
     │
     ▼
Router (classifies intent)
     │
     ├──► Summarizer ──────► summarize_paper tool
     ├──► Citation ────────► extract_citations tool
     ├──► SemanticSearch ──► semantic_search tool
     ├──► ResearchGap ─────► detect_research_gaps tool
     ├──► KnowledgeGraph ──► build_knowledge_graph tool
     ├──► PaperComparison ─► compare_papers tool
     └──► Memory ──────────► Redis store
```
