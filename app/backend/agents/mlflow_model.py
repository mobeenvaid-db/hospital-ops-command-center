# Capacity Command — MLflow PyFunc Wrapper for MAS Supervisor
#
# Packages the Multi-Agent Supervisor as an MLflow model for deployment
# to Databricks Model Serving. The pyfunc interface accepts chat-style
# JSON input and returns structured agent results.
#
# Deployment:
#   1. Log model: python -m agents.mlflow_model --log
#   2. Deploy:    databricks serving-endpoints create --json @endpoint.json
#
# Input format (Model Serving chat/completions style):
#   {"messages": [{"role": "user", "content": "What's our bed capacity?"}]}
#
# Output format:
#   {"system_status": "WARNING", "routing": {...}, "agent_results": [...]}

import asyncio
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# ── Ensure imports work when loaded by MLflow outside FastAPI ──────────
# MLflow loads the model in an isolated environment. We need to ensure
# the app/backend directory is on sys.path so agents/ and uc_functions/
# can be imported.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """Get the running event loop or create a new one for sync contexts."""
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


class HospitalOpsSupervisorModel:
    """MLflow pyfunc model wrapping the MAS Supervisor.

    This class follows the MLflow pyfunc interface: load_context() for
    initialization, predict() for inference. It manages its own asyncpg
    connection pool and OAuth token lifecycle.
    """

    def __init__(self):
        self.supervisor = None
        self.pool = None
        self._initialized = False

    def load_context(self, context: Any = None) -> None:
        """Called by MLflow when the model is loaded.

        Initializes the database connection pool and creates the
        Supervisor instance. Uses the same dual-auth pattern as the
        FastAPI app (Databricks Apps SP vs local CLI profile).
        """
        if self._initialized:
            return

        loop = _get_or_create_event_loop()
        loop.run_until_complete(self._async_init())
        self._initialized = True

    async def _async_init(self) -> None:
        """Async initialization: create DB pool and Supervisor."""
        import asyncpg
        from config import get_oauth_token, get_workspace_host, SCHEMA, SERVING_ENDPOINT

        # Build connection parameters from environment
        pg_host = os.environ.get("PGHOST", "localhost")
        pg_port = int(os.environ.get("PGPORT", "5432"))
        pg_database = os.environ.get(
            "LAKEBASE_DATABASE",
            os.environ.get("PGDATABASE", "hospital_ops"),
        )
        pg_user = os.environ.get("PGUSER", "")

        # Get OAuth token for Lakebase auth
        token = get_oauth_token()
        workspace_host = get_workspace_host()

        # Create asyncpg pool with OAuth password
        self.pool = await asyncpg.create_pool(
            host=pg_host,
            port=pg_port,
            database=pg_database,
            user=pg_user,
            password=token,
            ssl="require",
            min_size=1,
            max_size=4,
            server_settings={
                "search_path": f"{SCHEMA}, public",
            },
        )

        # Import supervisor (after sys.path is set)
        from agents.supervisor import Supervisor

        # Create a connection for the supervisor
        conn = await self.pool.acquire()

        self.supervisor = Supervisor(
            conn=conn,
            schema=SCHEMA,
            fmapi_url=workspace_host,
            fmapi_token=token,
            serving_endpoint=SERVING_ENDPOINT,
        )
        self._conn = conn
        logger.info("HospitalOpsSupervisorModel initialized: schema=%s", SCHEMA)

    def predict(self, context: Any = None, model_input: Any = None) -> dict:
        """MLflow pyfunc predict interface.

        Accepts chat-style input and routes through the MAS Supervisor.

        Parameters
        ----------
        context : mlflow.pyfunc.PythonModelContext, optional
            MLflow context (unused after load_context).
        model_input : dict or pandas.DataFrame
            Input in chat/completions format:
            {"messages": [{"role": "user", "content": "..."}]}

            Or direct query format:
            {"query": "What's our bed capacity?"}

        Returns
        -------
        dict
            Supervisor response with system_status, routing, and agent_results.
        """
        if not self._initialized:
            self.load_context()

        # Extract query from input
        query = self._extract_query(model_input)
        if not query:
            return {
                "system_status": "error",
                "error": "No query provided. Send {'messages': [{'role': 'user', 'content': '...'}]}",
            }

        # Run the supervisor (sync wrapper around async)
        loop = _get_or_create_event_loop()
        try:
            result = loop.run_until_complete(self.supervisor.route(query))
        except RuntimeError:
            # If we're already in an async context, use a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    asyncio.run, self.supervisor.route(query)
                )
                result = future.result(timeout=60)

        return result

    def _extract_query(self, model_input: Any) -> str:
        """Extract the user query from various input formats."""
        if model_input is None:
            return ""

        # Direct dict input
        if isinstance(model_input, dict):
            # Chat completions format
            messages = model_input.get("messages", [])
            if messages:
                # Get the last user message
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        return msg.get("content", "")
                # Fallback to last message
                return messages[-1].get("content", "")

            # Direct query format
            if "query" in model_input:
                return model_input["query"]

            # Dataframe-like single row
            if "content" in model_input:
                return model_input["content"]

        # Pandas DataFrame (MLflow default)
        try:
            import pandas as pd
            if isinstance(model_input, pd.DataFrame):
                if "messages" in model_input.columns:
                    messages = model_input.iloc[0]["messages"]
                    if isinstance(messages, str):
                        messages = json.loads(messages)
                    for msg in reversed(messages):
                        if msg.get("role") == "user":
                            return msg.get("content", "")
                elif "query" in model_input.columns:
                    return str(model_input.iloc[0]["query"])
                elif "content" in model_input.columns:
                    return str(model_input.iloc[0]["content"])
        except (ImportError, Exception):
            pass

        # String input
        if isinstance(model_input, str):
            return model_input

        return ""


# ── MLflow model logging ──────────────────────────────────────────────

def log_model(
    registered_model_name: str = "hospital-ops-agent-supervisor",
    artifact_path: str = "supervisor-model",
) -> str:
    """Log the MAS Supervisor as an MLflow model.

    Returns the model URI for deployment.
    """
    import mlflow
    from mlflow.models.signature import ModelSignature
    from mlflow.types.schema import ColSpec, Schema

    # Define input/output schema
    input_schema = Schema([
        ColSpec("string", "messages"),  # JSON-encoded messages array
    ])
    output_schema = Schema([
        ColSpec("string", "response"),  # JSON-encoded response
    ])
    signature = ModelSignature(inputs=input_schema, outputs=output_schema)

    # Input example
    input_example = {
        "messages": json.dumps([
            {"role": "user", "content": "What's our bed capacity gap for the next 4 hours?"}
        ])
    }

    # Pip requirements (match app/requirements.txt + agents deps)
    pip_requirements = [
        "asyncpg>=0.30.0",
        "openai>=1.50.0",
        "databricks-sdk>=0.36.0",
        "pyyaml>=6.0",
        "mlflow>=2.10.0",
    ]

    # Code paths to include with the model
    code_paths = [
        os.path.join(_BACKEND_DIR, "agents"),
        os.path.join(_BACKEND_DIR, "uc_functions"),
        os.path.join(_BACKEND_DIR, "config.py"),
    ]
    # Filter to only existing paths
    code_paths = [p for p in code_paths if os.path.exists(p)]

    mlflow.set_experiment("hospital_ops_agents")

    with mlflow.start_run(run_name="supervisor-model-registration") as run:
        mlflow.set_tags({
            "model_type": "multi-agent-supervisor",
            "agents": "bed_capacity,or_utilization,discharge,anomaly",
            "framework": "langgraph",
        })

        model_info = mlflow.pyfunc.log_model(
            artifact_path=artifact_path,
            python_model=HospitalOpsSupervisorModel(),
            registered_model_name=registered_model_name,
            signature=signature,
            input_example=input_example,
            pip_requirements=pip_requirements,
            code_paths=code_paths if code_paths else None,
        )

        model_uri = f"models:/{registered_model_name}/latest"
        logger.info("Model logged: %s (run_id=%s)", model_uri, run.info.run_id)
        return model_uri


# ── CLI entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MAS Supervisor MLflow Model")
    parser.add_argument("--log", action="store_true", help="Log model to MLflow")
    parser.add_argument(
        "--model-name",
        default="hospital-ops-agent-supervisor",
        help="Registered model name (default: hospital-ops-agent-supervisor)",
    )
    args = parser.parse_args()

    if args.log:
        logging.basicConfig(level=logging.INFO)
        uri = log_model(registered_model_name=args.model_name)
        print(f"Model URI: {uri}")
    else:
        parser.print_help()
