"""Agent execution endpoints."""

from fastapi import APIRouter, Depends
from backend.api.not_implemented import not_implemented
from backend.api.schemas.agents import AgentRunRequest, AgentRunResponse
from backend.services.agent_service import AgentService
from backend.security.api_security import require_permission
from backend.security.rbac import Permission
from shared.models.principal import Principal

router = APIRouter()


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(
    body: AgentRunRequest,
    principal: Principal = Depends(require_permission(Permission.AGENT_RUN)),
    service: AgentService = Depends(),
) -> AgentRunResponse:
    """Execute an agent task through the orchestrator — M5."""
    raise not_implemented()


@router.get("/status/{session_id}")
async def get_agent_status(
    session_id: str,
    # AGENT_RUN, not a read permission, and this is an assumption worth stating:
    # the scaffold defines no `agent:read`, and a task's status is only
    # meaningful to a role that could have started one. Viewers cannot run
    # agents, so they have no task whose status they could ask about. Inventing
    # a seventh permission to express that would be speculative.
    principal: Principal = Depends(require_permission(Permission.AGENT_RUN)),
) -> None:
    """Check the status of a running agent task — M5."""
    raise not_implemented()
