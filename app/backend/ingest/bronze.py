"""Raw payload landing (bronze).

Every received payload is written here verbatim before mapping, so you keep a
durable, replayable record of exactly what the EHR sent.

Two implementations, selected by config:
  • DeltaBronzeSink  — appends to a Delta table via a SQL warehouse (Databricks).
  • LocalFileSink    — appends NDJSON to a local file (local dev / off-platform).
  • NullSink         — drops the record (explicitly disabled).

Landing is best-effort: a bronze failure is logged but never blocks the
serving upsert, which is what keeps the dashboard live.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class BronzeSink:
    async def write(self, record: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class NullSink(BronzeSink):
    async def write(self, record: dict) -> None:
        return None


class LocalFileSink(BronzeSink):
    """Append one JSON object per line. Good for local dev and debugging."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    async def write(self, record: dict) -> None:
        line = json.dumps(record, default=str)
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with open(self.path, "a") as f:
            f.write(line + "\n")


class DeltaBronzeSink(BronzeSink):
    """Append to a Delta table via the SQL warehouse statement execution API."""

    def __init__(self, catalog: str, schema: str, warehouse_id: str, parse_variant: bool = True):
        self.table = f"{catalog}.{schema}.redox_events_raw"
        self.warehouse_id = warehouse_id
        self.parse_variant = parse_variant

    async def write(self, record: dict) -> None:
        await asyncio.to_thread(self._insert, record)

    def _insert(self, record: dict) -> None:
        from databricks.sdk.service.sql import StatementParameterListItem

        from config import get_workspace_client

        variant_expr = "try_parse_json(:payload)" if self.parse_variant else "NULL"
        stmt = (
            f"INSERT INTO {self.table} "
            "(ingest_id, received_at, source_format, data_model, event_type, "
            " redox_message_id, source_name, payload, payload_variant, headers, "
            " processed, process_error) "
            "SELECT :ingest_id, current_timestamp(), :source_format, :data_model, "
            " :event_type, :redox_message_id, :source_name, :payload, "
            f"{variant_expr}, map(), :processed, :process_error"
        )

        def p(name: str, value) -> StatementParameterListItem:
            if value is None:
                return StatementParameterListItem(name=name, value=None)
            return StatementParameterListItem(name=name, value=str(value))

        params = [
            p("ingest_id", record.get("ingest_id")),
            p("source_format", record.get("source_format")),
            p("data_model", record.get("data_model")),
            p("event_type", record.get("event_type")),
            p("redox_message_id", record.get("redox_message_id")),
            p("source_name", record.get("source_name")),
            p("payload", record.get("payload")),
            StatementParameterListItem(
                name="processed", value=str(bool(record.get("processed"))).lower(), type="BOOLEAN"
            ),
            p("process_error", record.get("process_error")),
        ]
        w = get_workspace_client()
        w.statement_execution.execute_statement(
            warehouse_id=self.warehouse_id,
            statement=stmt,
            parameters=params,
            wait_timeout="30s",
        )


_sink: Optional[BronzeSink] = None


def get_bronze_sink() -> BronzeSink:
    """Build the configured sink once (singleton)."""
    global _sink
    if _sink is not None:
        return _sink

    mode = os.environ.get("BRONZE_SINK", "").lower()
    if not mode:
        # auto: Delta when a warehouse + catalog are configured, else local file
        if os.environ.get("WAREHOUSE_ID") and os.environ.get("DELTA_CATALOG"):
            mode = "delta"
        else:
            mode = "local"

    if mode == "delta":
        _sink = DeltaBronzeSink(
            catalog=os.environ["DELTA_CATALOG"],
            schema=os.environ.get("DELTA_BRONZE_SCHEMA", "hospital_ops_bronze"),
            warehouse_id=os.environ["WAREHOUSE_ID"],
            parse_variant=os.environ.get("BRONZE_PARSE_VARIANT", "true").lower() == "true",
        )
    elif mode == "none":
        _sink = NullSink()
    else:
        _sink = LocalFileSink(os.environ.get("BRONZE_LOCAL_PATH", "/tmp/redox_bronze.ndjson"))
    logger.info("Bronze sink: %s", type(_sink).__name__)
    return _sink
