# Capacity Command — Dual-mode authentication config
#
# Detects if running inside a Databricks App (auto-injected service principal)
# or locally (uses Databricks CLI profile for auth).

import os
import re

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

_SCHEMA_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_workspace_client = None


def get_workspace_client():
    """Singleton WorkspaceClient — reused across all pool connections and API calls."""
    global _workspace_client
    if _workspace_client is not None:
        return _workspace_client

    from databricks.sdk import WorkspaceClient

    if IS_DATABRICKS_APP:
        _workspace_client = WorkspaceClient()
    else:
        profile = os.environ.get("DATABRICKS_PROFILE", "DEFAULT")
        _workspace_client = WorkspaceClient(profile=profile)
    return _workspace_client


def get_oauth_token() -> str:
    """Get OAuth token for Lakebase / FMAPI authentication."""
    client = get_workspace_client()
    auth_headers = client.config.authenticate()
    if auth_headers and "Authorization" in auth_headers:
        return auth_headers["Authorization"].replace("Bearer ", "")
    return client.config.token or ""


def get_workspace_host() -> str:
    """Get workspace host URL with https:// prefix."""
    if IS_DATABRICKS_APP:
        # IMPORTANT: DATABRICKS_HOST in Databricks Apps is just hostname, no scheme
        host = os.environ.get("DATABRICKS_HOST", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    client = get_workspace_client()
    return client.config.host  # SDK includes https://


# Data source mode: "lakebase" (asyncpg) or "delta" (SQL warehouse)
DATA_SOURCE = os.environ.get("DATA_SOURCE", "lakebase").lower()
if DATA_SOURCE not in ("lakebase", "delta"):
    raise ValueError(f"DATA_SOURCE must be 'lakebase' or 'delta', got: {DATA_SOURCE!r}")

# Schema for queries
if DATA_SOURCE == "lakebase":
    _raw_schema = os.environ.get("LAKEBASE_SCHEMA", "hospital_ops_lakebase")
else:  # delta
    _raw_schema = os.environ.get("DELTA_CATALOG", "main") + "." + os.environ.get("DELTA_SCHEMA", "hospital_ops")

SCHEMA = _raw_schema

# SQL Warehouse ID (required for delta mode)
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", "")
if DATA_SOURCE == "delta" and not WAREHOUSE_ID:
    raise ValueError("WAREHOUSE_ID required when DATA_SOURCE=delta")

# Serving endpoint for Foundation Model API
SERVING_ENDPOINT = os.environ.get("SERVING_ENDPOINT", "databricks-claude-sonnet-4-5")

# ── Realtime vs. simulation ─────────────────────────────────────────
# Realtime deployments are driven by the Redox ingestion pipeline (the default).
# The synthetic simulator (control/lifecycle routes + Databricks seed/simulate
# jobs) is opt-in for demos only.
ENABLE_SIMULATION = os.environ.get("ENABLE_SIMULATION", "false").lower() == "true"

# Facility name shown in the dashboard header / assistant context.
SITE_NAME = os.environ.get("SITE_NAME", "General Hospital")

# Allow ingestion to create bed rows it sees but that aren't in the bed master.
AUTO_CREATE_BEDS = os.environ.get("AUTO_CREATE_BEDS", "false").lower() == "true"
