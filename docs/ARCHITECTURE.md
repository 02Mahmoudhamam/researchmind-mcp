# ResearchMind MCP — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (Next.js)                   │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP/REST
┌───────────────────────▼─────────────────────────────────┐
│               FastAPI Backend (Port 8000)                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐  │
│  │   Auth   │  │ Documents│  │  Agents  │  │ Search │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘  │
└───────────────────────┬─────────────────────────────────┘
                        │ MCP Protocol
┌───────────────────────▼─────────────────────────────────┐
│                    MCP Server (Port 8001)                │
│          Tools │ Resources │ Prompts                     │
└───────────────────────┬─────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
┌───────▼──────┐ ┌──────▼──────┐ ┌─────▼──────┐
│ Orchestrator │ │   Qdrant    │ │   Redis    │
│    Agent     │ │  (Vectors)  │ │  (Memory)  │
│              │ └─────────────┘ └────────────┘
│  ┌─────────┐ │
│  │ Router  │ │
│  └────┬────┘ │
└───────┼───────┘
        │
   ┌────┴──────────────────────────────────┐
   │              Specialized Agents        │
   ├──────────────┬──────────────┬──────────┤
   │  Summarizer  │   Citation   │  Search  │
   │  ResearchGap │  KnowGraph   │ Compare  │
   └──────────────┴──────────────┴──────────┘
```

## Layer Responsibilities

| Layer | Responsibility |
|-------|---------------|
| Frontend | User interface, file upload, agent chat |
| FastAPI | REST API, auth, request routing |
| MCP Server | Tool/resource/prompt registry |
| Orchestrator | Decompose user intent, coordinate agents |
| Router | Select the right agent for each subtask |
| Agents | Specialized AI tasks via Claude API |
| Qdrant | Vector similarity search |
| Redis | Session memory and caching |
| Document Processing | PDF → chunks → embeddings |
