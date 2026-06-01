# Capacity Command — DB tests (TDD: written FIRST before implementation)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.backend.db import DatabasePool, db_pool


# ── create_pool timeouts ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_pool_has_command_timeout():
    """create_pool is called with command_timeout=30 and timeout=10."""
    with patch("app.backend.db.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = MagicMock()
        with patch.dict("os.environ", {"PGHOST": "localhost", "PGPASSWORD": "test"}):
            pool = DatabasePool()
            await pool.get_pool()
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs.get("command_timeout") == 30
        assert call_kwargs.get("timeout") == 10


# ── Token refresh backoff ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_refresh_backoff_on_failure():
    """Exponential backoff on consecutive failures: 30s, 60s, 120s. Reset on success."""
    import asyncio

    backoff_seen = []
    stop_event = asyncio.Event()
    real_sleep = asyncio.sleep

    async def tracking_sleep(delay):
        if delay in (30, 60, 120):
            backoff_seen.append(delay)
        if stop_event.is_set():
            raise asyncio.CancelledError()  # Hard stop to prevent infinite spin
        await real_sleep(0)  # Yield without recursion

    with patch("app.backend.db.IS_DATABRICKS_APP", True):
        pool = DatabasePool()
        pool._pool = MagicMock()

        with patch.object(pool, "refresh_token", new_callable=AsyncMock) as mock_refresh:
            with patch("app.backend.db.asyncio.sleep", side_effect=tracking_sleep):
                mock_refresh.side_effect = [
                    Exception("fail1"),
                    Exception("fail2"),
                    Exception("fail3"),
                    None,
                ]

                task = asyncio.create_task(pool.start_token_refresh_loop())
                # Poll until we've had 4 refresh attempts (3 fail + 1 success)
                for _ in range(500):
                    await asyncio.sleep(0.01)
                    if mock_refresh.call_count >= 4:
                        break
                stop_event.set()  # Signal tracking_sleep to raise CancelledError
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                assert 30 in backoff_seen, f"Expected 30 in backoff_seen, got {backoff_seen}"
                assert 60 in backoff_seen, f"Expected 60 in backoff_seen, got {backoff_seen}"
                assert 120 in backoff_seen, f"Expected 120 in backoff_seen, got {backoff_seen}"
