"""Load one or more CSV/Parquet files into a per-investigation DuckDB database.

Security model: each *source* file is read exactly once, at load time, on a
read-write connection that has filesystem access, and materialized into its own
DuckDB table. All subsequent query connections are opened **read-only with
external access disabled**, so generated SQL can never write, attach, or read
arbitrary files — it can only read (and join) the materialized tables.

Multiple tables live in one database file, so cross-table JOINs work while the
whole thing stays read-only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import duckdb

DEFAULT_TABLE_NAME = "dataset"
DATA_SUFFIXES = {".csv", ".parquet", ".pq"}


# --------------------------------------------------------------------------- #
# Postgres snapshot import (the automated version of "export to Parquet, point
# Exhibit at it"): copy selected tables read-only into the per-investigation DuckDB.
# The trust/reproducibility model is UNCHANGED — after import, every query still runs
# against local tables on a read-only, external-access-off connection.
# --------------------------------------------------------------------------- #

def is_postgres_dsn(s) -> bool:
    return isinstance(s, str) and s.startswith(("postgres://", "postgresql://"))


def redact_dsn(dsn: str) -> str:
    """Strip credentials (and query) from a DSN for display/provenance."""
    try:
        p = urlsplit(dsn)
        netloc = (p.hostname or "") + (f":{p.port}" if p.port else "")
        return urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:
        return "postgres"


def _sql_str(s: str) -> str:
    """Escape a value for inlining as a single-quoted SQL string. (DuckDB's ATTACH does
    not accept bound parameters, so the DSN/path must be inlined.)"""
    return s.replace("'", "''")


def _pg_connect():
    """A read-write, external-access-ON DuckDB connection with the postgres extension
    loaded. Used only for the one-time snapshot import — never for query execution."""
    con = duckdb.connect()  # in-memory; external access on by default
    con.execute("INSTALL postgres")
    con.execute("LOAD postgres")
    return con


def postgres_catalog(dsn: str) -> List[Dict[str, str]]:
    """List user tables in the Postgres database (names only — cheap; no counts)."""
    con = _pg_connect()
    try:
        con.execute(f"ATTACH '{_sql_str(dsn)}' AS pg (TYPE postgres, READ_ONLY)")
        rows = con.execute(
            "SELECT table_schema, table_name FROM pg.information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_type = 'BASE TABLE' ORDER BY 1, 2"
        ).fetchall()
        return [{"schema": r[0], "table": r[1]} for r in rows]
    finally:
        con.close()


def postgres_row_counts(dsn: str, selected: List[Tuple[str, str]]) -> List[Dict]:
    """Exact row counts for the SELECTED tables only — shown for consent before copying."""
    con = _pg_connect()
    try:
        con.execute(f"ATTACH '{_sql_str(dsn)}' AS pg (TYPE postgres, READ_ONLY)")
        out = []
        for schema, table in selected:
            try:
                n = con.execute(f'SELECT count(*) FROM pg."{schema}"."{table}"').fetchone()[0]
            except Exception:
                n = None
            out.append({"schema": schema, "table": table, "rows": n})
        return out
    finally:
        con.close()


def add_postgres_table(
    duckdb_path: Path, dsn: str, schema: str, table: str, local_name: str,
    max_rows: Optional[int] = None,
) -> Tuple[Dict[str, str], int, int]:
    """Snapshot one Postgres table into the local DuckDB. Returns
    (schema_map, source_row_count, imported_row_count). Applies ``max_rows`` as a cap."""
    con = _pg_connect()
    try:
        con.execute(f"ATTACH '{_sql_str(dsn)}' AS pg (TYPE postgres, READ_ONLY)")
        con.execute(f"ATTACH '{_sql_str(str(duckdb_path))}' AS local")  # the investigation db
        src = f'pg."{schema}"."{table}"'
        source_rows = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
        limit = f" LIMIT {int(max_rows)}" if max_rows else ""
        con.execute(f'CREATE TABLE local."{local_name}" AS SELECT * FROM {src}{limit}')
        imported = con.execute(f'SELECT count(*) FROM local."{local_name}"').fetchone()[0]
        smap = con.execute(f'DESCRIBE local."{local_name}"').fetchall()
        return {r[0]: r[1] for r in smap}, int(source_rows), int(imported)
    finally:
        con.close()


def expand_sources(paths) -> List[Path]:
    """Expand any directories into their .csv/.parquet files (sorted), keep files
    as-is, and de-duplicate by resolved path while preserving order. A directory
    contributes every data file directly inside it (non-recursive)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    seen: Set[Path] = set()
    out: List[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.suffix.lower() in DATA_SUFFIXES)
            if not files:
                raise ValueError(f"no .csv or .parquet files found in {p}")
            candidates = files
        else:
            candidates = [p]
        for c in candidates:
            key = c.resolve() if c.exists() else c
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in (".parquet", ".pq"):
        return "parquet"
    raise ValueError(f"Unsupported file type: {suffix!r} (expected .csv or .parquet)")


def safe_table_name(source: Path, existing: Set[str]) -> str:
    """Derive a unique, SQL-safe table name from a file name."""
    stem = re.sub(r"[^a-z0-9_]+", "_", source.stem.lower()).strip("_")
    if not stem or stem[0].isdigit():
        stem = f"t_{stem}" if stem else "table"
    name = stem
    i = 2
    while name in existing:
        name = f"{stem}_{i}"
        i += 1
    return name


def create_database(duckdb_path: Path) -> None:
    """Start a fresh database file (removing any existing one)."""
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    if duckdb_path.exists():
        duckdb_path.unlink()


# CSV read attempts, fastest/most-typed first. read_csv_auto samples only the
# first ~20k rows to infer types, so a value that violates the guess further down
# (e.g. "-" in a column sniffed as BIGINT) errors. We fall back to full-file type
# inference, then to loading everything as text so a messy file still loads.
_CSV_READ_ATTEMPTS = [
    "read_csv_auto(?)",                    # fast: sampled type detection
    "read_csv_auto(?, sample_size=-1)",    # robust: scan the whole file for types
    "read_csv_auto(?, all_varchar=true)",  # last resort: load every column as text
]


def add_table(duckdb_path: Path, source: Path, fmt: str, table_name: str) -> Dict[str, str]:
    """Materialize ``source`` into ``table_name`` in the database. Returns the
    schema map (column name -> DuckDB type). Opens its own read-write connection."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    con = duckdb.connect(str(duckdb_path))
    try:
        if fmt == "csv":
            _create_from_csv(con, table_name, source)
        else:
            con.execute(
                f'CREATE TABLE "{table_name}" AS SELECT * FROM read_parquet(?)', [str(source)]
            )
        schema = con.execute(f'DESCRIBE "{table_name}"').fetchall()
    finally:
        con.close()
    return {row[0]: row[1] for row in schema}


def _create_from_csv(con: duckdb.DuckDBPyConnection, table_name: str, source: Path) -> None:
    last: Exception = None
    for expr in _CSV_READ_ATTEMPTS:
        try:
            con.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM {expr}', [str(source)])
            return
        except duckdb.Error as e:
            last = e
            con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    raise ValueError(f"could not parse CSV {source.name}: {last}")


def open_readonly(duckdb_path: Path) -> duckdb.DuckDBPyConnection:
    """Open the materialized database read-only with external access disabled."""
    return duckdb.connect(
        str(duckdb_path),
        read_only=True,
        config={"enable_external_access": False},
    )


def load(source: Path, duckdb_path: Path) -> Tuple[str, str, Dict[str, str]]:
    """Convenience single-file loader: fresh DB, one table named ``dataset``.
    Returns ``(format, table_name, schema)``."""
    fmt = detect_format(source)
    create_database(duckdb_path)
    schema = add_table(duckdb_path, source, fmt, DEFAULT_TABLE_NAME)
    return fmt, DEFAULT_TABLE_NAME, schema
