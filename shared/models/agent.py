"""Agent I/O models."""

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
from datetime import datetime


class AgentInput(BaseModel):
    session_id: str
    user_id: str
    task: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    context: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    agent_name: str
    session_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    tokens_used: int = 0
    latency_ms: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AgentConfig(BaseModel):
    name: str
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.3
    timeout_seconds: int = 120
    retry_attempts: int = 3
    enabled: bool = True
