# ResearchMind MCP

> Production-grade AI Research Assistant built on the Model Context Protocol (MCP).

## Overview

ResearchMind MCP enables researchers to upload papers, PDFs, and technical documents,
then leverage a multi-agent AI system to analyze, summarize, compare, and extract
knowledge from their research corpus.

## Architecture

```
User → MCP Client → Orchestrator Agent → Router → Specialized Agents
                                                 ↓
                                    Vector DB (Qdrant) + Memory (Redis)
```

## Quick Start

```bash
cp .env.example .env
docker-compose up -d
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API Reference](docs/API.md)
- [Agent Guide](docs/AGENTS.md)
- [MCP Guide](docs/MCP.md)

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, MCP SDK |
| Agents | Claude API, AsyncIO |
| Vector DB | Qdrant |
| Memory | Redis |
| Frontend | Next.js, TypeScript, Tailwind |
| DevOps | Docker, docker-compose |
