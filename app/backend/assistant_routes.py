# Capacity Command — AI Assistant API routes
#
# Uses config.py for workspace host + OAuth token (dual-mode auth).

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from config import SCHEMA, SERVING_ENDPOINT, get_workspace_host, get_oauth_token, IS_DATABRICKS_APP
from assistant import AssistantAgent, AssistantContextBuilder
from db import get_db

import os

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    context_queries: list[str]
    data_citations: list[str]


class ShiftSummaryResponse(BaseModel):
    summary: str
    sections: dict
    generated_at: str


def _get_fmapi_url() -> str:
    """Get FMAPI base URL from workspace host."""
    try:
        return get_workspace_host()
    except Exception:
        # Fallback for local dev without SDK configured
        host = os.environ.get("FMAPI_HOST", "")
        if host and not host.startswith("http"):
            return f"https://{host}"
        return host or ""


def _get_fmapi_token() -> str:
    """Get FMAPI auth token (OAuth in Databricks Apps, fallback to env var)."""
    try:
        return get_oauth_token()
    except Exception:
        # Fallback for local dev
        return os.environ.get("FMAPI_TOKEN", "")


@router.post("/ask", response_model=AskResponse)
async def assistant_ask(request: AskRequest, db=Depends(get_db)):
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    context_builder = AssistantContextBuilder(db, SCHEMA)
    agent = AssistantAgent(
        context_builder=context_builder,
        fmapi_url=_get_fmapi_url(),
        fmapi_token=_get_fmapi_token(),
        serving_endpoint=SERVING_ENDPOINT,
    )
    result = await agent.ask(request.question)
    return AskResponse(**result)


@router.post("/shift-summary", response_model=ShiftSummaryResponse)
async def assistant_shift_summary(db=Depends(get_db)):
    context_builder = AssistantContextBuilder(db, SCHEMA)
    agent = AssistantAgent(
        context_builder=context_builder,
        fmapi_url=_get_fmapi_url(),
        fmapi_token=_get_fmapi_token(),
        serving_endpoint=SERVING_ENDPOINT,
    )
    result = await agent.shift_summary()
    return ShiftSummaryResponse(**result)
