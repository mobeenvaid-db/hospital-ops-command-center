# Capacity Command — Dual-mode SQL executor
#
# Executes SQL queries against either:
# - Lakebase (Postgres) via asyncpg
# - Delta tables (Unity Catalog) via SQL warehouse
#
# Provides a unified async interface for both backends.

from typing import Any, Optional
import asyncio
from functools import wraps

from config import DATA_SOURCE, WAREHOUSE_ID, get_workspace_client


class SqlExecutor:
    """Unified SQL executor for Lakebase or Delta."""

    def __init__(self, conn: Any = None):
        """
        Args:
            conn: For lakebase mode, an asyncpg.Connection.
                  For delta mode, ignored (uses workspace client).
        """
        self.conn = conn
        self.mode = DATA_SOURCE

    async def fetchrow(self, query: str, *args) -> Optional[dict]:
        """Execute query and return single row as dict (or None)."""
        if self.mode == "lakebase":
            if self.conn is None:
                return None
            row = await self.conn.fetchrow(query, *args)
            if row is None:
                return None
            return dict(row)

        else:  # delta
            # Convert asyncpg $1, $2 params to Python format for SQL warehouse
            formatted_query = self._format_query_for_warehouse(query, args)

            # Execute via SQL warehouse
            w = get_workspace_client()
            result = await asyncio.to_thread(
                w.statement_execution.execute_statement,
                warehouse_id=WAREHOUSE_ID,
                statement=formatted_query,
                wait_timeout="50s"
            )

            if not result.result or not result.result.data_array:
                return None

            # Convert first row to dict
            row_data = result.result.data_array[0]
            columns = [col.name for col in result.manifest.schema.columns]
            return dict(zip(columns, row_data))

    async def fetch(self, query: str, *args) -> list[dict]:
        """Execute query and return all rows as list of dicts."""
        if self.mode == "lakebase":
            if self.conn is None:
                return []
            rows = await self.conn.fetch(query, *args)
            return [dict(row) for row in rows]

        else:  # delta
            formatted_query = self._format_query_for_warehouse(query, args)

            w = get_workspace_client()
            result = await asyncio.to_thread(
                w.statement_execution.execute_statement,
                warehouse_id=WAREHOUSE_ID,
                statement=formatted_query,
                wait_timeout="50s"
            )

            if not result.result or not result.result.data_array:
                return []

            # Convert all rows to dicts
            columns = [col.name for col in result.manifest.schema.columns]
            return [dict(zip(columns, row)) for row in result.result.data_array]

    async def fetchval(self, query: str, *args) -> Any:
        """Execute query and return single value from first row, first column."""
        if self.mode == "lakebase":
            if self.conn is None:
                return None
            return await self.conn.fetchval(query, *args)

        else:  # delta
            formatted_query = self._format_query_for_warehouse(query, args)

            w = get_workspace_client()
            result = await asyncio.to_thread(
                w.statement_execution.execute_statement,
                warehouse_id=WAREHOUSE_ID,
                statement=formatted_query,
                wait_timeout="50s"
            )

            if not result.result or not result.result.data_array:
                return None

            return result.result.data_array[0][0]

    def _format_query_for_warehouse(self, query: str, args: tuple) -> str:
        """Convert asyncpg $1, $2 placeholders to literal values for SQL warehouse."""
        if not args:
            return query

        formatted = query
        for i, arg in enumerate(args, 1):
            placeholder = f"${i}"
            if isinstance(arg, str):
                # Escape single quotes
                safe_arg = arg.replace("'", "''")
                replacement = f"'{safe_arg}'"
            elif arg is None:
                replacement = "NULL"
            elif isinstance(arg, (int, float)):
                replacement = str(arg)
            else:
                replacement = f"'{str(arg)}'"

            formatted = formatted.replace(placeholder, replacement)

        return formatted
