"""Read-only SQL guard — the highest-risk component, so defense in depth.

Execution-time safety is already provided by opening DuckDB read-only with
``enable_external_access=False`` (see ``data/loader.py``). This module adds a
*static* layer that runs before execution:

1. Parse with sqlglot; require **exactly one** statement.
2. Require the root to be a ``SELECT`` / set-operation of selects. Any
   INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/ATTACH/COPY/PRAGMA/SET/CALL/... has a
   non-select root and is rejected.
3. Reject file-reading table functions (``read_csv``, ``read_parquet``,
   ``glob``, ...) anywhere in the tree — only the materialized table is allowed.
4. Ensure a top-level ``LIMIT`` (wrap if absent).

Rejections raise ``SqlGuardError``.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from ..config import DEFAULT_ROW_LIMIT

_DIALECT = "duckdb"

# Function names that read from the filesystem / network. Blocked even though the
# read-only connection would also refuse them — belt and suspenders.
FORBIDDEN_FUNCTIONS = {
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "parquet_scan",
    "read_json",
    "read_json_auto",
    "read_json_objects",
    "read_text",
    "read_blob",
    "glob",
    "sniff_csv",
    "delta_scan",
    "iceberg_scan",
    "postgres_scan",
    "sqlite_scan",
}

# Statement/expression node types that must never appear. Built by name so the
# guard tolerates sqlglot versions that lack some of these classes.
_FORBIDDEN_TYPE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "TruncateTable", "Command", "Copy", "Attach", "Detach", "Set", "Pragma",
    "Use", "Grant", "Transaction", "Commit", "Rollback",
    # DuckDB file readers that sqlglot models as dedicated nodes (not Anonymous).
    "ReadCSV", "ReadParquet",
]
_FORBIDDEN_TYPES = tuple(
    getattr(exp, name) for name in _FORBIDDEN_TYPE_NAMES if hasattr(exp, name)
)

_SELECT_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)


class SqlGuardError(ValueError):
    """Raised when SQL is not a safe, read-only single SELECT."""


def guard_sql(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> str:
    """Validate ``sql`` and return a safe, LIMIT-bounded equivalent."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=_DIALECT) if s is not None]
    except sqlglot.errors.ParseError as e:  # pragma: no cover - message passthrough
        raise SqlGuardError(f"could not parse SQL: {e}") from e

    if len(statements) == 0:
        raise SqlGuardError("no SQL statement found")
    if len(statements) > 1:
        raise SqlGuardError("only a single statement is allowed")

    stmt = statements[0]

    if not isinstance(stmt, _SELECT_ROOTS):
        raise SqlGuardError(
            f"only read-only SELECT queries are allowed (got {type(stmt).__name__})"
        )

    forbidden = list(stmt.find_all(*_FORBIDDEN_TYPES)) if _FORBIDDEN_TYPES else []
    if forbidden:
        raise SqlGuardError(
            f"forbidden statement type: {type(forbidden[0]).__name__}"
        )

    for func in stmt.find_all(exp.Anonymous, exp.Func):
        name = (func.name or "").lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlGuardError(f"forbidden function: {name}")

    return _ensure_limit(stmt, limit)


def _ensure_limit(stmt: exp.Expression, limit: int) -> str:
    if isinstance(stmt, exp.Select) and stmt.args.get("limit") is None:
        return stmt.limit(limit).sql(dialect=_DIALECT)
    if isinstance(stmt, exp.Select):
        return stmt.sql(dialect=_DIALECT)
    # Set operation or subquery root: wrap so a LIMIT is guaranteed.
    return f"SELECT * FROM ({stmt.sql(dialect=_DIALECT)}) AS _exhibit_q LIMIT {limit}"
