"""Redox endpoint verification + request authentication.

Redox secures destination webhooks two ways; we support both:

  1. Verification handshake — on setup, Redox sends a GET with
     `verification-token` and `challenge` query params. We echo the challenge
     (as text/plain) only if the token matches REDOX_VERIFICATION_TOKEN.

  2. Per-request auth — each POST carries either the shared `verification-token`
     header and/or a `Redox-Signature` HMAC (base64 HMAC-SHA256 of the raw body
     using REDOX_SIGNATURE_SECRET). We verify whichever is configured.

If neither secret is configured, requests are allowed but a warning is logged —
convenient for local dev. Set REQUIRE_INGEST_AUTH=true to hard-fail instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)


def _verification_token() -> str:
    return os.environ.get("REDOX_VERIFICATION_TOKEN", "")


def _signature_secret() -> str:
    return os.environ.get("REDOX_SIGNATURE_SECRET", "")


def _require_auth() -> bool:
    return os.environ.get("REQUIRE_INGEST_AUTH", "false").lower() == "true"


def verify_challenge(token: str | None, challenge: str | None) -> tuple[bool, str]:
    """Validate the setup-time verification handshake.

    Returns (ok, body_to_return). When ok, body_to_return is the challenge.
    """
    expected = _verification_token()
    if not expected:
        # Nothing configured — accept so setup can complete in dev.
        logger.warning("REDOX_VERIFICATION_TOKEN not set; accepting handshake unverified")
        return True, challenge or ""
    if token and hmac.compare_digest(token, expected):
        return True, challenge or ""
    return False, ""


def verify_request(headers: dict, raw_body: bytes) -> tuple[bool, str]:
    """Authenticate an inbound POST. Returns (ok, reason)."""
    # normalize header keys to lower-case
    h = {k.lower(): v for k, v in headers.items()}

    token = _verification_token()
    secret = _signature_secret()

    if not token and not secret:
        if _require_auth():
            return False, "no ingest credentials configured but REQUIRE_INGEST_AUTH=true"
        logger.warning("Ingest auth not configured; accepting request unverified")
        return True, "unverified"

    # shared-secret header
    if token:
        provided = h.get("verification-token") or h.get("x-verification-token")
        if provided and hmac.compare_digest(provided, token):
            return True, "token"

    # HMAC signature
    if secret:
        provided_sig = h.get("redox-signature") or h.get("x-redox-signature")
        if provided_sig:
            expected_sig = base64.b64encode(
                hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
            ).decode()
            if hmac.compare_digest(provided_sig.strip(), expected_sig):
                return True, "signature"

    return False, "verification failed"
