"""Tool abstraction + validation helpers.

A ``Tool`` declares a name, a human/LLM-facing description, and a Pydantic
``Input`` schema (which doubles as the function-calling schema for the real
LLM). ``run`` executes deterministically against the read-only connection and
returns a ``ToolResult``. Input/precondition problems raise ``ToolError`` so the
orchestrator can record a clean ``error`` node instead of crashing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Type

import duckdb
from pydantic import BaseModel, ValidationError

from ..models import ColumnProfile, DatasetProfile, ToolResult


class ToolError(ValueError):
    """Raised for invalid inputs or unmet statistical preconditions."""


class Tool(ABC):
    name: str
    description: str
    Input: Type[BaseModel]

    @abstractmethod
    def run(
        self,
        con: duckdb.DuckDBPyConnection,
        table_name: str,
        profile: DatasetProfile,
        inputs: BaseModel,
    ) -> ToolResult:
        ...

    def parse_inputs(self, raw: Dict[str, Any]) -> BaseModel:
        try:
            return self.Input.model_validate(raw)
        except ValidationError as e:
            raise ToolError(f"invalid inputs for '{self.name}': {e}") from e

    def spec(self) -> Dict[str, Any]:
        """LLM-facing description used for tool selection / function calling."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.Input.model_json_schema(),
        }


# --------------------------------------------------------------------------- #
# shared validation / query helpers
# --------------------------------------------------------------------------- #

_NUMERIC_TYPES = ("INT", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "REAL")


def get_column(profile: DatasetProfile, name: str) -> ColumnProfile:
    for c in profile.columns:
        if c.name == name:
            return c
    available = ", ".join(c.name for c in profile.columns)
    raise ToolError(f"column '{name}' not found. Available: {available}")


def require_numeric(profile: DatasetProfile, name: str) -> ColumnProfile:
    col = get_column(profile, name)
    if not any(t in col.dtype.upper() for t in _NUMERIC_TYPES):
        raise ToolError(f"column '{name}' is {col.dtype}, expected a numeric type")
    return col


def fetch_floats(
    con: duckdb.DuckDBPyConnection, table_name: str, column: str
) -> List[float]:
    rows = con.execute(
        f'SELECT "{column}" FROM "{table_name}" WHERE "{column}" IS NOT NULL'
    ).fetchall()
    return [float(r[0]) for r in rows]


def fetch_columns(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: List[str],
    drop_nulls: bool = True,
) -> List[list]:
    """Fetch several columns as rows (optionally dropping rows with any null)."""
    sel = ", ".join(f'"{c}"' for c in columns)
    where = (
        " AND ".join(f'"{c}" IS NOT NULL' for c in columns) if drop_nulls else "1=1"
    )
    rows = con.execute(f'SELECT {sel} FROM "{table_name}" WHERE {where}').fetchall()
    return [list(r) for r in rows]


def require_min_rows(n: int, need: int, what: str = "rows") -> None:
    if n < need:
        raise ToolError(f"need at least {need} {what}, got {n}")
