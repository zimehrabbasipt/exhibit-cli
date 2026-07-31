"""Filesystem artifact paths, keyed by node id.

Artifacts (chart PNGs, spilled parquet tables) are large/binary and belong on
the filesystem, not in SQLite. Paths are deterministic functions of the
investigation + node id so they're easy to locate and, later, serve to a UI.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AppPaths


def chart_path(paths: AppPaths, investigation_id: str, node_id: str) -> Path:
    paths.ensure_investigation_dirs(investigation_id)
    return paths.charts_dir(investigation_id) / f"{node_id}.png"


def table_path(paths: AppPaths, investigation_id: str, node_id: str) -> Path:
    paths.ensure_investigation_dirs(investigation_id)
    return paths.tables_dir(investigation_id) / f"{node_id}.parquet"
