"""FastAPI routes for the Redox ingestion webhook.

  GET  /api/ingest/redox   -> Redox verification handshake (echo challenge)
  POST /api/ingest/redox   -> receive ADT (Redox Data Model JSON or FHIR)
  GET  /api/ingest/health  -> ingest subsystem status

The POST path: authenticate -> land raw to bronze -> normalize -> apply to
Lakebase (one transaction per message) -> record in ingest_log (idempotent).
It always returns quickly; mapping errors are captured per-message, not raised
to Redox (which would otherwise retry the whole batch).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, PlainTextResponse

import db
from config import SCHEMA

from .applier import apply_event
from .bronze import get_bronze_sink
from .location_map import LocationMap
from .normalize import UnsupportedPayload, detect_format, normalize
from .security import verify_challenge, verify_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

# Loaded once at import; cheap and stateless.
_locmap = LocationMap.load()


def _auto_create_beds() -> bool:
    return os.environ.get("AUTO_CREATE_BEDS", "false").lower() == "true"


@router.get("/redox")
async def redox_verify(request: Request):
    """Redox setup-time verification handshake."""
    token = request.query_params.get("verification-token")
    challenge = request.query_params.get("challenge")
    ok, body = verify_challenge(token, challenge)
    if not ok:
        return PlainTextResponse("verification failed", status_code=401)
    return PlainTextResponse(body, status_code=200)


@router.get("/health")
async def ingest_health():
    return {
        "status": "ok",
        "auto_create_beds": _auto_create_beds(),
        "bronze_sink": type(get_bronze_sink()).__name__,
        "location_map_loaded": True,
    }


@router.post("/redox")
async def redox_ingest(request: Request, conn=Depends(db.get_db)):
    raw = await request.body()
    ok, reason = verify_request(dict(request.headers), raw)
    if not ok:
        logger.warning("Rejected ingest request: %s", reason)
        return JSONResponse({"error": "unauthorized", "reason": reason}, status_code=401)

    if conn is None:
        return JSONResponse(
            {"error": "database not configured (set PGHOST/PGDATABASE for realtime ingest)"},
            status_code=503,
        )

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)

    # A per-delivery message id (when the sender provides one as a header) is a
    # reliable idempotency key. Redox includes a delivery/message id header.
    h = {k.lower(): v for k, v in request.headers.items()}
    header_msg_id = h.get("redox-message-id") or h.get("x-redox-message-id") or h.get("x-message-id")

    # Redox sends one message per POST, but tolerate a list.
    messages = body if isinstance(body, list) else [body]
    results = []
    for msg in messages:
        results.append(await _handle_one(conn, msg, header_msg_id))

    applied = sum(1 for r in results if r.get("status") == "applied")
    return {"received": len(messages), "applied": applied, "results": results}


async def _handle_one(conn: Any, payload: dict, header_msg_id: str | None = None) -> dict:
    fmt = detect_format(payload)
    bronze = get_bronze_sink()
    ingest_id = uuid.uuid4().hex

    # Try to normalize first so the bronze record is well-labeled.
    try:
        event = normalize(payload)
    except UnsupportedPayload as e:
        await _safe_bronze(bronze, ingest_id, payload, fmt, None, None, None, None,
                           processed=False, error=str(e))
        await _log_ingest(conn, None, fmt, None, None, "error", str(e))
        return {"status": "error", "detail": str(e)}

    # Idempotency key: payload-carried message id (Redox Data Model Meta.Message.ID)
    # or a per-delivery header. NOT the encounter/visit id, which is stable across
    # a stay. When neither exists, dedup is skipped and state-idempotency applies.
    msg_id = event.redox_message_id or header_msg_id

    # Idempotency: skip messages we've already applied.
    if msg_id:
        seen = await conn.fetchval(
            f"SELECT 1 FROM {SCHEMA}.ingest_log WHERE redox_message_id = $1 AND status = 'applied'",
            msg_id,
        )
        if seen:
            return {"status": "skipped", "detail": "duplicate message", "redox_message_id": msg_id}

    await _safe_bronze(bronze, ingest_id, payload, event.source_format, event.data_model,
                       event.source_event_label, msg_id, event.source_name,
                       processed=False, error=None)

    try:
        async with conn.transaction():
            result = await apply_event(conn, SCHEMA, event, _locmap, _auto_create_beds())
        status = result.get("status", "applied")
        await _log_ingest(conn, msg_id, event.source_format, event.data_model,
                          event.source_event_label, status, result.get("detail"))
        result["redox_message_id"] = msg_id
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to apply event %s", msg_id)
        await _log_ingest(conn, msg_id, event.source_format, event.data_model,
                          event.source_event_label, "error", str(e))
        return {"status": "error", "detail": str(e), "redox_message_id": msg_id}


async def _safe_bronze(bronze, ingest_id, payload, fmt, data_model, event_type,
                       msg_id, source_name, processed, error) -> None:
    try:
        await bronze.write({
            "ingest_id": ingest_id,
            "source_format": fmt,
            "data_model": data_model,
            "event_type": event_type,
            "redox_message_id": msg_id,
            "source_name": source_name,
            "payload": json.dumps(payload),
            "processed": processed,
            "process_error": error,
        })
    except Exception:  # noqa: BLE001 - bronze landing must never block serving
        logger.exception("Bronze write failed (continuing)")


async def _log_ingest(conn, msg_id, fmt, data_model, event_type, status, detail) -> None:
    try:
        await conn.execute(
            f"""
            INSERT INTO {SCHEMA}.ingest_log
                (redox_message_id, source_format, data_model, event_type, status, detail)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (redox_message_id) WHERE redox_message_id IS NOT NULL
            DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail, received_at = now()
            """,
            msg_id, fmt, data_model, event_type, status, detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ingest_log write failed (continuing)")
