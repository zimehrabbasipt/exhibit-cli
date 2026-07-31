"""SQLite connection + schema.

The store holds only *metadata*: investigations and the append-only node log.
User data lives in DuckDB; large artifacts live on the filesystem. We keep the
schema tiny and use ``content_json`` for kind-specific node payloads so adding a
new node kind never requires a migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    data_path       TEXT NOT NULL,
    data_format     TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    schema_json     TEXT NOT NULL,
    profile_node_id TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id),
    seq              INTEGER NOT NULL,
    parent_id        TEXT REFERENCES nodes(id),
    kind             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    title            TEXT NOT NULL,
    content_json     TEXT NOT NULL,
    artifact_path    TEXT,
    status           TEXT NOT NULL DEFAULT 'ok',
    error            TEXT,
    model            TEXT,
    prompt_version   TEXT
);

CREATE INDEX IF NOT EXISTS idx_nodes_inv_seq
    ON nodes (investigation_id, seq);

-- Typed analytical edges (the DAG that lives alongside the conversational tree).
-- ``parent_id`` on nodes carries conversational order; edges carry analytical
-- dependency/relationship. ``created_by`` records provenance so a check can tell a
-- deterministic engine edge from an LLM-asserted one.
CREATE TABLE IF NOT EXISTS edges (
    id               TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id),
    source_id        TEXT NOT NULL REFERENCES nodes(id),
    target_id        TEXT NOT NULL REFERENCES nodes(id),
    relationship     TEXT NOT NULL,   -- EdgeType
    created_by       TEXT NOT NULL,   -- engine | narrator | judge | user
    status           TEXT NOT NULL DEFAULT 'active',  -- active | proposed | rejected | superseded
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_inv     ON edges (investigation_id);
CREATE INDEX IF NOT EXISTS idx_edges_source  ON edges (source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target  ON edges (target_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and ensure the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
