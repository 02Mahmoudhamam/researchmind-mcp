"""Agent request/response schemas."""
from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class AgentRunRequest(BaseModel):
    task: str
    document_ids: List[str] = []
    agent_name: Optional[str] = None
    session_id: Optional[str] = None
    options: Dict[str, Any] = {}


class AgentRunResponse(BaseModel):
    session_id: str
    agent_name: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    latency_ms: float
