"""Append-only node log over SQLite.

Nodes are only ever appended. The one mutation we allow is filling in an
``artifact_path`` after a node's artifact (e.g. a chart PNG or spilled parquet)
has been written to disk — the logical content of the node is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from pydantic import TypeAdapter

from ..models import Node, NodeKind, NodePayload, NodeStatus

_payload_adapter: TypeAdapter = TypeAdapter(NodePayload)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_seq(conn: sqlite3.Connection, investigation_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM nodes WHERE investigation_id = ?",
        (investigation_id,),
    ).fetchone()
    return int(row["m"]) + 1


def _row_to_node(row: sqlite3.Row) -> Node:
    payload = _payload_adapter.validate_python(json.loads(row["content_json"]))
    return Node(
        id=row["id"],
        investigation_id=row["investigation_id"],
        seq=row["seq"],
        parent_id=row["parent_id"],
        kind=NodeKind(row["kind"]),
        created_at=row["created_at"],
        title=row["title"],
        payload=payload,
        artifact_path=row["artifact_path"],
        status=NodeStatus(row["status"]),
        error=row["error"],
        model=row["model"],
        prompt_version=row["prompt_version"],
    )


def append(
    conn: sqlite3.Connection,
    investigation_id: str,
    payload: NodePayload,
    title: str,
    *,
    parent_id: Optional[str] = None,
    status: NodeStatus = NodeStatus.ok,
    error: Optional[str] = None,
    artifact_path: Optional[str] = None,
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> Node:
    """Append a new node and return the persisted model."""
    node = Node(
        id=uuid4().hex,
        investigation_id=investigation_id,
        seq=_next_seq(conn, investigation_id),
        parent_id=parent_id,
        kind=payload.kind,
        created_at=_now(),
        title=title,
        payload=payload,
        artifact_path=artifact_path,
        status=status,
        error=error,
        model=model,
        prompt_version=prompt_version,
    )
    conn.execute(
        """
        INSERT INTO nodes
            (id, investigation_id, seq, parent_id, kind, created_at, title,
             content_json, artifact_path, status, error, model, prompt_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node.id,
            node.investigation_id,
            node.seq,
            node.parent_id,
            node.kind.value,
            node.created_at,
            node.title,
            node.payload.model_dump_json(),
            node.artifact_path,
            node.status.value,
            node.error,
            node.model,
            node.prompt_version,
        ),
    )
    conn.commit()
    return node


def set_artifact_path(conn: sqlite3.Connection, node_id: str, artifact_path: str) -> None:
    conn.execute(
        "UPDATE nodes SET artifact_path = ? WHERE id = ?", (artifact_path, node_id)
    )
    conn.commit()


def get(conn: sqlite3.Connection, node_id: str) -> Optional[Node]:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return _row_to_node(row) if row else None


def find_by_prefix(conn: sqlite3.Connection, investigation_id: str, prefix: str) -> List[Node]:
    """Resolve a short id prefix (what users type) to matching nodes."""
    rows = conn.execute(
        "SELECT * FROM nodes WHERE investigation_id = ? AND id LIKE ? ORDER BY seq",
        (investigation_id, prefix + "%"),
    ).fetchall()
    return [_row_to_node(r) for r in rows]


def list_by_investigation(conn: sqlite3.Connection, investigation_id: str) -> List[Node]:
    rows = conn.execute(
        "SELECT * FROM nodes WHERE investigation_id = ? ORDER BY seq",
        (investigation_id,),
    ).fetchall()
    return [_row_to_node(r) for r in rows]


def children(conn: sqlite3.Connection, parent_id: str) -> List[Node]:
    rows = conn.execute(
        "SELECT * FROM nodes WHERE parent_id = ? ORDER BY seq", (parent_id,)
    ).fetchall()
    return [_row_to_node(r) for r in rows]


def recent(conn: sqlite3.Connection, investigation_id: str, limit: int) -> List[Node]:
    rows = conn.execute(
        "SELECT * FROM nodes WHERE investigation_id = ? ORDER BY seq DESC LIMIT ?",
        (investigation_id, limit),
    ).fetchall()
    return list(reversed([_row_to_node(r) for r in rows]))
