> [!WARNING]
> **TARGET STATE — NOT CURRENT BEHAVIOUR.**
> This document describes the system as intended, not as implemented. As of the
> repository baseline, the described behaviour does not execute. See
> [AUDIT-2026-09.md](AUDIT-2026-09.md) for the verified current state and
> [../roadmap/MILESTONES.md](../roadmap/MILESTONES.md) for the delivery plan.

# MCP Integration Guide

## Tools

| Tool | Description | Input |
|------|-------------|-------|
| summarize_paper | Generates paper summary | document_ids |
| extract_citations | Extracts all references | document_ids |
| compare_papers | Side-by-side comparison | document_ids (2+) |
| semantic_search | Vector similarity search | query, limit |
| build_knowledge_graph | Entity-relationship graph | document_ids |
| detect_research_gaps | Literature gap analysis | document_ids |
| generate_research_questions | Question generation | document_ids |

## Resources

| URI Pattern | Content |
|------------|---------|
| research://pdf/{id} | Raw PDF document |
| research://paper/{id} | Parsed paper with metadata |
| research://notes/{id} | User research notes |
| research://metadata/{id} | Document metadata |
| research://graph/{id} | Knowledge graph data |

## Prompts

| Name | Purpose |
|------|---------|
| summarization_prompt | Structured paper summary |
| scientific_reviewer_prompt | Peer review simulation |
| citation_extraction_prompt | Citation parsing |
| research_gap_prompt | Gap identification |
| paper_comparison_prompt | Comparative analysis |
| question_generation_prompt | Research question ideation |
