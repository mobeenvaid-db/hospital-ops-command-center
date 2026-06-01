# Capacity Command — Database connection (Lakebase or Delta)
#
# - Lakebase mode: asyncpg pool for Postgres with SSL + OAuth
# - Delta mode: No pool (uses SQL warehouse via workspace client)
#
# Authenticates via OAuth token from Databricks SDK (auto-refreshed).
# Falls back to password-based auth for local dev with PGPASSWORD set.

import asyncio
import logging
import os
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

from config import IS_DATABRICKS_APP, DATA_SOURCE, get_oauth_token


class DatabasePool:
    """Manages an asyncpg connection pool with automatic OAuth token refresh."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._demo_mode = False
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_lock: Optional[asyncio.Lock] = None

    async def get_pool(self) -> Optional[asyncpg.Pool]:
        """Create or return the connection pool. Returns None in demo mode."""
        # Check if Lakebase is configured
        if not os.environ.get("PGHOST"):
            self._demo_mode = True
            return None

        if self._pool is None:
            try:
                password = self._get_password()
                ssl_mode = "require" if IS_DATABRICKS_APP else (
                    "require" if os.environ.get("PGSSLMODE") else None
                )
                db_name = os.environ.get("LAKEBASE_DATABASE",
                          os.environ.get("PGDATABASE", "postgres"))
                logger.info("Connecting to Lakebase database=%s schema=%s",
                            db_name, os.environ.get("LAKEBASE_SCHEMA", "?"))
                self._pool = await asyncpg.create_pool(
                    host=os.environ["PGHOST"],
                    port=int(os.environ.get("PGPORT", "5432")),
                    database=db_name,
                    user=os.environ.get("PGUSER", "postgres"),
                    password=password,
                    ssl=ssl_mode,
                    min_size=2,
                    max_size=10,
                    max_inactive_connection_lifetime=300,  # Kill idle conns after 5min
                    command_timeout=30,
                    timeout=10,
                )
            except Exception as e:
                logger.error("Lakebase connection failed: %s", e)
                self._demo_mode = True
                return None
        return self._pool

    async def refresh_token(self):
        """Refresh the OAuth token by recreating the pool.

        Uses a lock so concurrent callers (e.g. 7 simultaneous API requests
        all hitting InvalidPasswordError) don't stampede.
        """
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            if self._pool:
                await self._pool.close()
                self._pool = None
            await self.get_pool()

    async def start_token_refresh_loop(self):
        """Background task to refresh OAuth token every 45 minutes."""
        if not IS_DATABRICKS_APP:
            return  # Only needed in Databricks Apps (OAuth tokens expire in 1hr)
        backoff_seconds = [30, 60, 120]
        consecutive_failures = 0
        while True:
            await asyncio.sleep(45 * 60)  # 45 minutes
            try:
                await self.refresh_token()
                consecutive_failures = 0
                logger.info("OAuth token refreshed")
            except Exception as e:
                logger.error("Token refresh failed: %s", e)
                wait = backoff_seconds[min(consecutive_failures, len(backoff_seconds) - 1)]
                consecutive_failures += 1
                await asyncio.sleep(wait)

    async def close(self):
        """Shut down the pool and refresh task."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def is_demo_mode(self) -> bool:
        return self._demo_mode

    @staticmethod
    def _get_password() -> str:
        """Get password: env var (if set), or OAuth token from Databricks SDK."""
        # Explicit password takes priority (e.g. service accounts, CI)
        explicit = os.environ.get("PGPASSWORD", os.environ.get("LAKEBASE_PASSWORD", ""))
        if explicit:
            return explicit
        # Fall back to OAuth token from Databricks SDK (works locally + in Apps)
        try:
            return get_oauth_token()
        except Exception:
            return ""


# Module-level singleton
db_pool = DatabasePool()


# ── FastAPI dependency ──────────────────────────────────────────────────

async def get_db():
    """FastAPI dependency: yields an asyncpg connection from the pool.

    If Lakebase is not configured (demo mode), yields None so endpoints
    can fall back to empty/default data via try/except in queries.

    On InvalidPasswordError (expired OAuth token), recreates the pool
    with a fresh token and retries once.
    """
    pool = await db_pool.get_pool()
    if pool is None:
        yield None
        return
    try:
        async with pool.acquire() as conn:
            yield conn
            return
    except asyncpg.exceptions.InvalidPasswordError:
        pass  # Fall through to retry below

    # Token expired — refresh pool and retry once
    logger.warning("OAuth token expired — refreshing pool")
    await db_pool.refresh_token()
    pool = await db_pool.get_pool()
    if pool is None:
        yield None
        return
    async with pool.acquire() as conn:
        yield conn


# ── Legacy compatibility (for tests that mock at the module level) ──────
def close_pool():
    """Synchronous wrapper for backwards compatibility with test cleanup."""
    pass  # Actual cleanup happens via lifespan in main.py
