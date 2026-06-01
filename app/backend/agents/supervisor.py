# Capacity Command — Multi-Agent Supervisor (MAS)
#
# Routes user queries to specialized LangGraph agents based on intent.
# Uses keyword matching for fast routing, falls back to LLM-based
# classification via FMAPI Claude Sonnet for ambiguous queries.
#
# Usage:
#   supervisor = Supervisor(conn, schema, fmapi_url, fmapi_token, serving_endpoint)
#   result = await supervisor.route("How many beds are available in ICU?")

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from openai import AsyncOpenAI

from agents.bed_capacity_planner import BedCapacityPlanner
from agents.or_utilization_optimizer import ORUtilizationOptimizer
from agents.discharge_coordinator import DischargeCoordinator
from agents.anomaly_pattern_detector import AnomalyPatternDetector

logger = logging.getLogger(__name__)

# Agent class registry
_AGENT_CLASSES = {
    "agents.bed_capacity_planner.BedCapacityPlanner": BedCapacityPlanner,
    "agents.or_utilization_optimizer.ORUtilizationOptimizer": ORUtilizationOptimizer,
    "agents.discharge_coordinator.DischargeCoordinator": DischargeCoordinator,
    "agents.anomaly_pattern_detector.AnomalyPatternDetector": AnomalyPatternDetector,
}

_CONFIG_PATH = Path(__file__).parent / "mas_config.yaml"


def _load_config() -> dict:
    """Load MAS configuration from YAML."""
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Intent detection ────────────────────────────────────────────────────


def _keyword_route(query: str, config: dict) -> Optional[list[str]]:
    """Fast keyword-based routing. Returns agent names or None if no match."""
    query_lower = query.lower()

    # Check multi-agent triggers first
    for trigger in config["routing"].get("multi_agent_triggers", []):
        if trigger.lower() in query_lower:
            return list(config["agents"].keys())

    # Check keyword rules using word-boundary-prefix matching to handle
    # stems (e.g. "discharg" matches "discharge", "discharged", "discharges")
    matched = set()
    for rule in config["routing"].get("keyword_rules", []):
        for kw in rule["keywords"]:
            kw_clean = kw.strip().lower()
            # Use \b at start for word boundary, allow suffix flexibility
            pattern = r"\b" + re.escape(kw_clean)
            if re.search(pattern, query_lower):
                matched.add(rule["agent"])
                break

    # Case-sensitive keywords (e.g. "OR" = operating room, not conjunction "or")
    for rule in config["routing"].get("case_sensitive_keywords", []):
        for kw in rule["keywords"]:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, query):  # case-sensitive on original query
                matched.add(rule["agent"])
                break

    return list(matched) if matched else None


async def _llm_classify(
    query: str,
    config: dict,
    fmapi_url: str,
    fmapi_token: str,
    serving_endpoint: str,
) -> dict:
    """Use FMAPI to classify query intent. Returns {"agents": [...], "params": {...}}."""
    prompt = config["routing"]["llm_classification_prompt"]

    try:
        client = AsyncOpenAI(
            api_key=fmapi_token,
            base_url=f"{fmapi_url.rstrip('/')}/serving-endpoints",
            timeout=15.0,
        )
        response = await client.chat.completions.create(
            model=serving_endpoint,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=config["intent_detection"].get("max_tokens", 256),
            temperature=config["intent_detection"].get("temperature", 0.0),
        )
        if response.choices:
            raw = response.choices[0].message.content or "{}"
            # Extract JSON from the response (handle markdown code blocks)
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        logger.warning("LLM classification failed, defaulting to all agents: %s", e)

    # Fallback: run all agents
    return {"agents": list(config["agents"].keys()), "params": {}}


# ── Parameter extraction ────────────────────────────────────────────────

# Known department names in hospital_ops_lakebase
_KNOWN_DEPTS = {"ICU", "MedSurg", "ED", "NICU", "Oncology", "Peds", "L&D", "Cardiac"}


def _extract_params(query: str, llm_params: Optional[dict] = None) -> dict:
    """Extract agent run parameters from the query text."""
    params: dict[str, Any] = {}

    # Department filter — use word boundary to avoid "ED" matching inside "BEDS"
    for dept in sorted(_KNOWN_DEPTS, key=len, reverse=True):  # longest first
        pattern = r"\b" + re.escape(dept) + r"\b"
        if re.search(pattern, query, re.IGNORECASE):
            params["dept_filter"] = dept
            break

    # Merge LLM-extracted params (LLM takes precedence for explicit extractions)
    if llm_params:
        for key, value in llm_params.items():
            if value is not None:
                params[key] = value

    return params


# ── Agent execution ─────────────────────────────────────────────────────


def _create_agent(agent_name: str, config: dict, conn: Any, schema: str) -> Any:
    """Instantiate an agent by name from config."""
    agent_def = config["agents"][agent_name]
    cls = _AGENT_CLASSES.get(agent_def["class"])
    if cls is None:
        raise ValueError(f"Unknown agent class: {agent_def['class']}")
    return cls(conn=conn, schema=schema)


async def _run_agent(
    agent_name: str, agent: Any, params: dict, config: dict
) -> dict:
    """Run a single agent with merged parameters. Returns result with metadata."""
    # Start with config defaults, override with extracted params
    run_params = dict(config["agents"][agent_name].get("run_params", {}))
    for key, value in params.items():
        if key in run_params:
            run_params[key] = value

    # Remove None values (let agent defaults apply)
    run_params = {k: v for k, v in run_params.items() if v is not None}

    try:
        result = await agent.run(**run_params)
        return {
            "agent": agent_name,
            "success": True,
            "result": result,
        }
    except Exception as e:
        logger.error("Agent %s failed: %s", agent_name, e)
        return {
            "agent": agent_name,
            "success": False,
            "error": str(e),
        }


# ── System status rollup ───────────────────────────────────────────────

_STATUS_PRIORITY = {"CRITICAL": 4, "WARNING": 3, "HIGH_VOLUME": 2, "MONITOR": 1, "NORMAL": 0, "LOW": 0, "CLEAR": 0}


def _compute_system_status(agent_results: list[dict]) -> str:
    """Derive overall system status from individual agent results."""
    max_priority = 0
    for ar in agent_results:
        if not ar.get("success"):
            continue
        status = ar.get("result", {}).get("status", "NORMAL")
        max_priority = max(max_priority, _STATUS_PRIORITY.get(status, 0))

    for status, priority in _STATUS_PRIORITY.items():
        if priority == max_priority:
            return status
    return "NORMAL"


# ── Supervisor class ────────────────────────────────────────────────────


class Supervisor:
    """Multi-Agent Supervisor that routes queries to specialized hospital ops agents.

    Workflow:
        1. Keyword match for fast routing
        2. LLM classification for ambiguous queries
        3. Execute target agent(s) in parallel
        4. Aggregate results with system status rollup
    """

    def __init__(
        self,
        conn: Any,
        schema: str,
        fmapi_url: str = "",
        fmapi_token: str = "",
        serving_endpoint: str = "databricks-claude-sonnet-4-5",
    ):
        self.conn = conn
        self.schema = schema
        self.fmapi_url = fmapi_url
        self.fmapi_token = fmapi_token
        self.serving_endpoint = serving_endpoint
        self.config = _load_config()

    async def route(self, query: str) -> dict:
        """Route a user query to the appropriate agent(s) and return aggregated results.

        Parameters
        ----------
        query : str
            Natural language question about hospital operations.

        Returns
        -------
        dict
            {
                "system_status": "CRITICAL" | "WARNING" | "NORMAL" | ...,
                "routing": {"method": "keyword" | "llm", "agents": [...]},
                "agent_results": [{agent, success, result}, ...],
                "timestamp": "ISO8601"
            }
        """
        # Step 1: Determine which agents to invoke
        routing_method = "keyword"
        llm_params: dict = {}

        agent_names = _keyword_route(query, self.config)

        if agent_names is None:
            # Step 2: Fall back to LLM classification
            routing_method = "llm"
            classification = await _llm_classify(
                query, self.config,
                self.fmapi_url, self.fmapi_token, self.serving_endpoint,
            )
            agent_names = classification.get("agents", list(self.config["agents"].keys()))
            llm_params = classification.get("params", {})

        # Validate agent names
        valid_agents = [a for a in agent_names if a in self.config["agents"]]
        if not valid_agents:
            valid_agents = list(self.config["agents"].keys())

        # Step 3: Extract parameters from query
        params = _extract_params(query, llm_params)

        # Step 4: Create and run agents
        max_parallel = self.config.get("aggregation", {}).get("max_parallel", 4)
        agents_to_run = valid_agents[:max_parallel]

        tasks = []
        for name in agents_to_run:
            agent = _create_agent(name, self.config, self.conn, self.schema)
            tasks.append(_run_agent(name, agent, params, self.config))

        agent_results = await asyncio.gather(*tasks)
        agent_results = list(agent_results)

        # Step 5: Compute system status rollup
        system_status = "NORMAL"
        if self.config.get("aggregation", {}).get("include_system_status", True):
            system_status = _compute_system_status(agent_results)

        return {
            "system_status": system_status,
            "routing": {
                "method": routing_method,
                "agents": agents_to_run,
                "params_extracted": params,
            },
            "agent_results": agent_results,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def route_single(self, agent_name: str, **kwargs) -> dict:
        """Directly invoke a specific agent by name (bypasses routing).

        Parameters
        ----------
        agent_name : str
            One of: bed_capacity_planner, or_utilization_optimizer,
            discharge_coordinator, anomaly_pattern_detector.
        **kwargs
            Parameters passed directly to the agent's run() method.

        Returns
        -------
        dict
            Agent result dict with success flag and result/error.
        """
        if agent_name not in self.config["agents"]:
            return {
                "agent": agent_name,
                "success": False,
                "error": f"Unknown agent: {agent_name}",
            }

        agent = _create_agent(agent_name, self.config, self.conn, self.schema)
        return await _run_agent(agent_name, agent, kwargs, self.config)
