"""Agent execution endpoints."""

from fastapi import APIRouter, Depends
from backend.api.schemas.agents import AgentRunRequest, AgentRunResponse
from backend.services.agent_service import AgentService
from backend.security.api_security import get_current_user
from shared.models.principal import Principal

router = APIRouter()


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(
    body: AgentRunRequest,
    principal: Principal = Depends(get_current_user),
    service: AgentService = Depends(),
):
    """Execute an agent task through the orchestrator."""
    ...  # TODO: implement


@router.get("/status/{session_id}")
async def get_agent_status(
    session_id: str, principal: Principal = Depends(get_current_user)
):
    """Check the status of a running agent task."""
    ...  # TODO: implement
