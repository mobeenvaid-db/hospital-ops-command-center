# Capacity Command — Agent Supervisor API routes
#
# POST /api/agents/query     - Send query to MAS supervisor
# GET  /api/agents/status    - Check MAS endpoint health
# GET  /api/agents/capabilities - List available agents

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from agent_client import AgentClient

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    context: Optional[dict] = None


class ToolCall(BaseModel):
    function: str = ""
    args: dict = {}
    result: Optional[str] = None


class AgentQueryResponse(BaseModel):
    answer: str
    agent_used: str
    tool_calls: list[dict] = []
    reasoning: str = ""
    plan: dict = {}


class AgentCapability(BaseModel):
    agent_id: str
    name: str
    description: str
    triggers: list[str]


class AgentStatusResponse(BaseModel):
    status: str
    endpoint: str
    state: Optional[str] = None
    http_status: Optional[int] = None
    error: Optional[str] = None


@router.post("/query", response_model=AgentQueryResponse)
async def agent_query(request: AgentQueryRequest):
    """Route a query to the Multi-Agent Supervisor."""
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    client = AgentClient()
    result = await client.query(request.query, request.context)
    return AgentQueryResponse(**result)


@router.get("/status", response_model=AgentStatusResponse)
async def agent_status():
    """Check if the MAS Model Serving endpoint is healthy."""
    client = AgentClient()
    result = await client.health_check()
    return AgentStatusResponse(**result)


@router.get("/capabilities", response_model=list[AgentCapability])
async def agent_capabilities():
    """List available agents and their capabilities."""
    return [AgentCapability(**c) for c in AgentClient.get_capabilities()]
