# Capacity Command — Multi-Agent Supervisor Client
#
# Calls the MAS Model Serving endpoint with OAuth token.
# Handles timeouts, retries, and structured response parsing.

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from config import get_workspace_host, get_oauth_token, IS_DATABRICKS_APP

logger = logging.getLogger(__name__)

# Agent metadata for /capabilities
AGENT_CAPABILITIES = [
    {
        "agent_id": "bed_capacity_planner",
        "name": "Bed Capacity Planner",
        "description": "Analyzes bed capacity gaps per department, predicts shortfalls, and recommends actions.",
        "triggers": ["bed", "capacity", "occupancy", "available beds", "census", "gap"],
    },
    {
        "agent_id": "or_utilization_optimizer",
        "name": "OR Utilization Optimizer",
        "description": "Optimizes OR scheduling, identifies gaps and overruns, suggests schedule changes.",
        "triggers": ["OR", "operating room", "surgery", "utilization", "FCOTS", "schedule"],
    },
    {
        "agent_id": "discharge_coordinator",
        "name": "Discharge Coordinator",
        "description": "Prioritizes discharges by LACE risk, predicted timing, and staffing needs.",
        "triggers": ["discharge", "LACE", "readmission", "send home", "release"],
    },
    {
        "agent_id": "anomaly_pattern_detector",
        "name": "Anomaly Pattern Detector",
        "description": "Detects recurring anomaly patterns, correlates incidents, and escalates threats.",
        "triggers": ["anomaly", "spike", "unusual", "pattern", "alert", "incident"],
    },
]

# Default endpoint name (overridden by MAS_ENDPOINT env var)
DEFAULT_MAS_ENDPOINT = "capacity-command-mas"


class AgentClient:
    """Client for the Multi-Agent Supervisor Model Serving endpoint."""

    def __init__(
        self,
        endpoint_name: Optional[str] = None,
        max_retries: int = 2,
        timeout_seconds: float = 60.0,
    ):
        self.endpoint_name = endpoint_name or os.environ.get(
            "MAS_ENDPOINT", DEFAULT_MAS_ENDPOINT
        )
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def _get_serving_url(self) -> str:
        """Build the Model Serving invocation URL."""
        host = get_workspace_host().rstrip("/")
        return f"{host}/serving-endpoints/{self.endpoint_name}/invocations"

    def _get_headers(self) -> dict[str, str]:
        """Build auth headers with OAuth token."""
        token = get_oauth_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def query(self, question: str, context: Optional[dict] = None) -> dict:
        """Send a query to the MAS supervisor and return the structured response.

        Parameters
        ----------
        question : str
            Natural language question for the supervisor to route.
        context : dict, optional
            Additional context (e.g. department filter, time range).

        Returns
        -------
        dict with keys:
            - answer: str (the agent's response text)
            - agent_used: str (which agent handled the query)
            - tool_calls: list[dict] (UC Functions / queries invoked)
            - reasoning: str (supervisor routing rationale)
            - plan: dict (structured data from the agent, if any)
        """
        payload = {
            "dataframe_split": {
                "columns": ["query", "context"],
                "data": [[question, context or {}]],
            }
        }

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._invoke(payload)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "MAS request attempt %d failed (%s), retrying in %ds",
                        attempt + 1, type(e).__name__, wait,
                    )
                    await asyncio.sleep(wait)
            except httpx.HTTPStatusError as e:
                # Don't retry 4xx errors
                if 400 <= e.response.status_code < 500:
                    return self._error_response(
                        f"Bad request to MAS endpoint: {e.response.status_code}"
                    )
                last_error = e
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)

        return self._error_response(
            f"MAS endpoint unavailable after {self.max_retries + 1} attempts: {last_error}"
        )

    async def _invoke(self, payload: dict) -> dict:
        """Make the HTTP call to Model Serving."""
        url = self._get_serving_url()
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return self._parse_response(resp.json())

    def _parse_response(self, raw: dict) -> dict:
        """Parse Model Serving response into structured format.

        Model Serving wraps responses in {"predictions": [...]} format.
        The MAS supervisor returns structured JSON with agent routing info.
        """
        predictions = raw.get("predictions")
        if predictions and isinstance(predictions, list) and len(predictions) > 0:
            result = predictions[0]
            if isinstance(result, dict):
                return {
                    "answer": result.get("answer", result.get("response", "")),
                    "agent_used": result.get("agent_used", "supervisor"),
                    "tool_calls": result.get("tool_calls", []),
                    "reasoning": result.get("reasoning", ""),
                    "plan": result.get("plan", {}),
                }
            # String response from simpler endpoints
            return {
                "answer": str(result),
                "agent_used": "supervisor",
                "tool_calls": [],
                "reasoning": "",
                "plan": {},
            }

        # Fallback: try to use the raw response directly
        if isinstance(raw, dict) and "answer" in raw:
            return raw

        return self._error_response("Unexpected response format from MAS endpoint")

    async def health_check(self) -> dict:
        """Check if the MAS endpoint is reachable and ready."""
        try:
            url = self._get_serving_url().replace("/invocations", "")
            headers = self._get_headers()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    state = data.get("state", {}).get("ready", "UNKNOWN")
                    return {
                        "status": "ready" if state == "READY" else "not_ready",
                        "endpoint": self.endpoint_name,
                        "state": state,
                    }
                return {
                    "status": "error",
                    "endpoint": self.endpoint_name,
                    "http_status": resp.status_code,
                }
        except Exception as e:
            return {
                "status": "unreachable",
                "endpoint": self.endpoint_name,
                "error": str(e),
            }

    @staticmethod
    def _error_response(message: str) -> dict:
        return {
            "answer": message,
            "agent_used": "error",
            "tool_calls": [],
            "reasoning": "",
            "plan": {},
        }

    @staticmethod
    def get_capabilities() -> list[dict]:
        """Return the list of available agent capabilities."""
        return AGENT_CAPABILITIES
