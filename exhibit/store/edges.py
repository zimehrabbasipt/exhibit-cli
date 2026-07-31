"""Typed analytical edges over SQLite.

Edges are the DAG that lives alongside the conversational node tree. Unlike
``parent_id`` (which records conversational order), an edge records an analytical
relationship — ``supports`` / ``tests`` / ``depends_on`` / ``alternative_to`` … —
and *who asserted it* (``created_by``), so a judge can distinguish a deterministic
engine edge from an LLM-asserted one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from ..models import Edge, EdgeCreatedBy, EdgeStatus, EdgeType


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        investigation_id=row["investigation_id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        relationship=EdgeType(row["relationship"]),
        created_by=EdgeCreatedBy(row["created_by"]),
        status=EdgeStatus(row["status"]),
        created_at=row["created_at"],
    )


def add_edge(
    conn: sqlite3.Connection,
    investigation_id: str,
    source_id: str,
    target_id: str,
    relationship: EdgeType,
    created_by: EdgeCreatedBy,
    status: EdgeStatus = EdgeStatus.active,
) -> Edge:
    edge = Edge(
        id=uuid4().hex,
        investigation_id=investigation_id,
        source_id=source_id,
        target_id=target_id,
        relationship=relationship,
        created_by=created_by,
        status=status,
        created_at=_now(),
    )
    conn.execute(
        "INSERT INTO edges (id, investigation_id, source_id, target_id, "
        "relationship, created_by, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (edge.id, edge.investigation_id, edge.source_id, edge.target_id,
         edge.relationship.value, edge.created_by.value, edge.status.value,
         edge.created_at),
    )
    conn.commit()
    return edge


def list_by_investigation(conn: sqlite3.Connection, investigation_id: str) -> List[Edge]:
    rows = conn.execute(
        "SELECT * FROM edges WHERE investigation_id = ? ORDER BY created_at",
        (investigation_id,),
    ).fetchall()
    return [_row_to_edge(r) for r in rows]


def edges_into(conn: sqlite3.Connection, target_id: str) -> List[Edge]:
    rows = conn.execute("SELECT * FROM edges WHERE target_id = ?", (target_id,)).fetchall()
    return [_row_to_edge(r) for r in rows]


def edges_from(conn: sqlite3.Connection, source_id: str) -> List[Edge]:
    rows = conn.execute("SELECT * FROM edges WHERE source_id = ?", (source_id,)).fetchall()
    return [_row_to_edge(r) for r in rows]
