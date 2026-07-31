"""Deterministic dataset profiling.

Profiling is intentionally *deterministic* (pure SQL aggregates, no LLM): it is
the ground-truth context every later step is built on. For each column we record
type, null fraction, distinct count, min/max, and a few sample values.
"""

from __future__ import annotations

from typing import Dict

import duckdb

from ..models import ColumnProfile, DatasetProfile

_SAMPLE_LIMIT = 5


def profile_dataset(
    con: duckdb.DuckDBPyConnection, table_name: str, schema: Dict[str, str]
) -> DatasetProfile:
    row_count = int(con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
    columns = []
    for name, dtype in schema.items():
        col = _profile_column(con, table_name, name, dtype, row_count)
        columns.append(col)
    return DatasetProfile(table_name=table_name, row_count=row_count, columns=columns)


def _profile_column(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    name: str,
    dtype: str,
    row_count: int,
) -> ColumnProfile:
    q = f'"{name}"'
    tbl = f'"{table_name}"'
    nulls, distinct = con.execute(
        f"SELECT COUNT(*) - COUNT({q}), COUNT(DISTINCT {q}) FROM {tbl}"
    ).fetchone()
    null_fraction = (nulls / row_count) if row_count else 0.0

    col_min = col_max = None
    try:
        col_min, col_max = con.execute(
            f"SELECT CAST(MIN({q}) AS VARCHAR), CAST(MAX({q}) AS VARCHAR) FROM {tbl}"
        ).fetchone()
    except duckdb.Error:
        # Non-orderable types (e.g. nested) — skip min/max.
        pass

    samples = con.execute(
        f"SELECT DISTINCT CAST({q} AS VARCHAR) FROM {tbl} "
        f"WHERE {q} IS NOT NULL LIMIT {_SAMPLE_LIMIT}"
    ).fetchall()

    return ColumnProfile(
        name=name,
        dtype=dtype,
        null_fraction=round(null_fraction, 4),
        distinct_count=int(distinct),
        min=None if col_min is None else str(col_min),
        max=None if col_max is None else str(col_max),
        sample_values=[str(r[0]) for r in samples],
    )
