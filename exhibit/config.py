"""Application paths and settings.

Everything Exhibit persists lives under a single home directory (``~/.exhibit`` by
default, overridable via ``EXHIBIT_HOME``):

    ~/.exhibit/
      exhibit.db                 # SQLite: investigations + nodes
      investigations/<inv-id>/
        artifacts/
          charts/               # chart PNGs (node-id keyed)
          tables/               # spilled result tables (parquet, node-id keyed)
        export.md               # latest markdown export

Keeping the layout deterministic and file-based is what makes an investigation
inspectable and, later, renderable by a notebook UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Bumped whenever a prompt template changes so persisted nodes record which
# prompt produced them (reproducibility / provenance).
PROMPT_VERSION = "v0.1"

# Default cap applied by the SQL guard when a query has no LIMIT.
DEFAULT_ROW_LIMIT = 1000

# Result tables larger than this (rows) are spilled to parquet instead of being
# stored inline in SQLite.
INLINE_ROW_THRESHOLD = 200


def home_dir() -> Path:
    """Root directory for all Exhibit state."""
    env = os.environ.get("EXHIBIT_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".exhibit"
    return root


@dataclass(frozen=True)
class AppPaths:
    """Resolved filesystem locations, created on demand."""

    root: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        root = home_dir()
        root.mkdir(parents=True, exist_ok=True)
        (root / "investigations").mkdir(exist_ok=True)
        return cls(root=root)

    @property
    def db_path(self) -> Path:
        return self.root / "exhibit.db"

    def investigation_dir(self, investigation_id: str) -> Path:
        return self.root / "investigations" / investigation_id

    def artifacts_dir(self, investigation_id: str) -> Path:
        return self.investigation_dir(investigation_id) / "artifacts"

    def charts_dir(self, investigation_id: str) -> Path:
        return self.artifacts_dir(investigation_id) / "charts"

    def tables_dir(self, investigation_id: str) -> Path:
        return self.artifacts_dir(investigation_id) / "tables"

    def export_path(self, investigation_id: str) -> Path:
        return self.investigation_dir(investigation_id) / "export.md"

    def ensure_investigation_dirs(self, investigation_id: str) -> None:
        self.charts_dir(investigation_id).mkdir(parents=True, exist_ok=True)
        self.tables_dir(investigation_id).mkdir(parents=True, exist_ok=True)
