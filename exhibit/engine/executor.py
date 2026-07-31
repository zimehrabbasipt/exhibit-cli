"""Execute guarded SQL against the read-only DuckDB connection.

The executor is pure with respect to the user data: it only reads. Turning a
large result into a persisted parquet artifact is done separately (by the
orchestrator, on an app-controlled writable connection) so the locked user-data
connection never gains write/filesystem powers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import duckdb

from ..models import ResultTable

PREVIEW_ROWS = 50


@dataclass
class ExecResult:
    columns: List[str]
    rows: List[List[Any]]  # full result (bounded by the guard's LIMIT)

    def to_result_table(self, parquet_path: Optional[str] = None) -> ResultTable:
        return ResultTable(
            columns=self.columns,
            rows_preview=[_jsonable(r) for r in self.rows[:PREVIEW_ROWS]],
            row_count=len(self.rows),
            parquet_path=parquet_path,
        )


def execute(con: duckdb.DuckDBPyConnection, sql: str) -> ExecResult:
    cur = con.execute(sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    rows = [list(r) for r in cur.fetchall()]
    return ExecResult(columns=columns, rows=rows)


def _jsonable(row: List[Any]) -> List[Any]:
    """Coerce DuckDB values (dates, decimals) to JSON-serializable primitives."""
    out = []
    for v in row:
        if v is None or isinstance(v, (str, int, float, bool)):
            out.append(v)
        else:
            out.append(str(v))
    return out
