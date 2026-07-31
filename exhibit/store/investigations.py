"""Investigation CRUD over SQLite."""

from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from ..models import Investigation


def _row_to_investigation(row: sqlite3.Row) -> Investigation:
    return Investigation(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        data_path=row["data_path"],
        data_format=row["data_format"],
        table_name=row["table_name"],
        schema_map=json.loads(row["schema_json"]),
        profile_node_id=row["profile_node_id"],
    )


def insert(conn: sqlite3.Connection, inv: Investigation) -> None:
    conn.execute(
        """
        INSERT INTO investigations
            (id, name, created_at, data_path, data_format, table_name,
             schema_json, profile_node_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inv.id,
            inv.name,
            inv.created_at,
            inv.data_path,
            inv.data_format,
            inv.table_name,
            json.dumps(inv.schema_map),
            inv.profile_node_id,
        ),
    )
    conn.commit()


def set_profile_node(conn: sqlite3.Connection, investigation_id: str, node_id: str) -> None:
    conn.execute(
        "UPDATE investigations SET profile_node_id = ? WHERE id = ?",
        (node_id, investigation_id),
    )
    conn.commit()


def get(conn: sqlite3.Connection, investigation_id: str) -> Optional[Investigation]:
    row = conn.execute(
        "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
    ).fetchone()
    return _row_to_investigation(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> Optional[Investigation]:
    row = conn.execute(
        "SELECT * FROM investigations WHERE name = ? ORDER BY created_at DESC LIMIT 1",
        (name,),
    ).fetchone()
    return _row_to_investigation(row) if row else None


def resolve(conn: sqlite3.Connection, id_or_name: str) -> Optional[Investigation]:
    """Look up by exact id first, then by name (most recent wins)."""
    return get(conn, id_or_name) or get_by_name(conn, id_or_name)


def list_all(conn: sqlite3.Connection) -> List[Investigation]:
    rows = conn.execute(
        "SELECT * FROM investigations ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_investigation(r) for r in rows]
