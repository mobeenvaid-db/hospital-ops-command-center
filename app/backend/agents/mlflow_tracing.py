# MLflow Tracing for Capacity Command Agents
#
# Adds structured tracing to all 4 LangGraph agents:
#   - BedCapacityPlanner
#   - ORUtilizationOptimizer
#   - DischargeCoordinator
#   - AnomalyPatternDetector
#
# Each agent.run() call creates an MLflow trace with:
#   - Parent span for the full agent execution
#   - Child spans for each graph node (fetch, compute, build)
#   - Tags: agent_name, query_type, timestamp, status
#   - Metrics: latency_ms, node_count, result_status

import functools
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    import mlflow
    MLFLOW_AVAILABLE = True
except ImportError:
    MLFLOW_AVAILABLE = False

logger = logging.getLogger(__name__)

EXPERIMENT_NAME = "hospital_ops_agents"

# Agent name constants
AGENT_BED_CAPACITY = "bed_capacity_planner"
AGENT_OR_OPTIMIZER = "or_utilization_optimizer"
AGENT_DISCHARGE = "discharge_coordinator"
AGENT_ANOMALY = "anomaly_pattern_detector"


def setup_mlflow_tracing():
    """Initialize MLflow experiment and enable autologging.

    Call once at application startup (e.g., in FastAPI lifespan).
    Safe to call when mlflow is not installed -- becomes a no-op.
    """
    if not MLFLOW_AVAILABLE:
        logger.warning("mlflow not installed -- tracing disabled")
        return

    mlflow.set_experiment(EXPERIMENT_NAME)
    # Enable LangChain autolog for any LangChain-based components
    try:
        mlflow.langchain.autolog(
            log_models=False,
            log_input_examples=False,
            log_model_signatures=False,
        )
    except Exception:
        # langchain autolog may fail if langchain is not installed;
        # we still get manual tracing via trace_agent_run
        pass
    logger.info("MLflow tracing initialized: experiment=%s", EXPERIMENT_NAME)


def trace_agent_run(agent_name: str, query_type: str = "default"):
    """Decorator that wraps an agent's run() method with MLflow tracing.

    Creates a parent span covering the full agent execution, logging:
      - Input parameters as tags
      - Output plan/report as artifacts
      - Latency, status, and error info

    Usage:
        @trace_agent_run("bed_capacity_planner", "capacity_plan")
        async def run(self, dept_filter=None):
            ...
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if not MLFLOW_AVAILABLE:
                return await fn(*args, **kwargs)

            start = time.perf_counter()
            timestamp = datetime.now(timezone.utc).isoformat()
            run_name = f"{agent_name}_{timestamp[:19].replace(':', '-')}"

            try:
                with mlflow.start_run(run_name=run_name, nested=True) as run:
                    # Tag the run
                    mlflow.set_tags({
                        "agent_name": agent_name,
                        "query_type": query_type,
                        "timestamp": timestamp,
                        "framework": "langgraph",
                    })

                    # Log input parameters
                    param_dict = _extract_params(fn, args, kwargs)
                    mlflow.log_params({
                        k: str(v)[:250] for k, v in param_dict.items()
                    })

                    # Execute the agent
                    result = await fn(*args, **kwargs)

                    # Log metrics
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
                    mlflow.log_metrics({
                        "latency_ms": round(elapsed_ms, 2),
                        "success": 1 if status != "error" else 0,
                    })
                    mlflow.set_tag("result_status", status)

                    # Log result summary (not full payload to avoid bloat)
                    if isinstance(result, dict):
                        summary = result.get("summary", {})
                        for key, val in summary.items():
                            if isinstance(val, (int, float)):
                                mlflow.log_metric(f"result_{key}", val)

                    return result

            except Exception as e:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.error("Agent %s failed: %s", agent_name, e)
                # Still try to log the failure
                try:
                    mlflow.log_metrics({
                        "latency_ms": round(elapsed_ms, 2),
                        "success": 0,
                    })
                    mlflow.set_tag("error", str(e)[:500])
                except Exception:
                    pass
                raise

        return wrapper
    return decorator


def _extract_params(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """Extract named parameters from function call for logging."""
    import inspect
    sig = inspect.signature(fn)
    params = {}
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    for name, value in bound.arguments.items():
        if name == "self":
            continue
        params[name] = value
    return params


def log_node_span(agent_name: str, node_name: str):
    """Context manager for tracing individual graph nodes within an agent run.

    Usage:
        with log_node_span("bed_capacity_planner", "fetch_bed_status"):
            state = await fetch_bed_status(state, conn, schema)
    """
    if not MLFLOW_AVAILABLE:
        import contextlib
        return contextlib.nullcontext()

    return mlflow.start_span(name=f"{agent_name}.{node_name}")
