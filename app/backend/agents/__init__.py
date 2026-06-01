# Capacity Command — LangGraph Agents + MAS Supervisor
#
# 4 specialized agents + Multi-Agent Supervisor that combine UC Functions
# (business logic) with DBSQL MCP stored procedures (data queries) via
# LangGraph state machines for structured hospital operations reasoning.
#
# Agents:
#   - bed_capacity_planner: gap analysis + capacity recommendations
#   - or_utilization_optimizer: OR schedule optimization
#   - discharge_coordinator: discharge prioritization
#   - anomaly_pattern_detector: recurring anomaly detection
#   - supervisor: multi-agent routing and aggregation
#
# MLflow tracing: all agents are instrumented via @trace_agent_run
# Call setup_mlflow_tracing() at app startup to enable.
#
# Deployment: mlflow_model.py wraps Supervisor as MLflow pyfunc for Model Serving.

from agents.bed_capacity_planner import BedCapacityPlanner
from agents.or_utilization_optimizer import ORUtilizationOptimizer
from agents.discharge_coordinator import DischargeCoordinator
from agents.anomaly_pattern_detector import AnomalyPatternDetector
from agents.supervisor import Supervisor
from agents.mlflow_tracing import setup_mlflow_tracing

__all__ = [
    "BedCapacityPlanner",
    "ORUtilizationOptimizer",
    "DischargeCoordinator",
    "AnomalyPatternDetector",
    "Supervisor",
    "setup_mlflow_tracing",
]
