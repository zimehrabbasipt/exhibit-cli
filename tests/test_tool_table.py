"""Tools select the table that holds their columns — not always the primary table."""

import pytest

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.engine.orchestrator import _resolve_tool_table
from exhibit.llm.mock import MockLLM
from exhibit.models import ColumnProfile, DatasetProfile, ToolResultPayload
from exhibit.store import db
from exhibit.tools.base import ToolError


def _p(name, cols):
    return DatasetProfile(table_name=name, row_count=10,
                          columns=[ColumnProfile(name=c, dtype="DOUBLE", null_fraction=0.0)
                                   for c in cols])


class _Sess:
    def __init__(self, profiles):
        self.profiles = profiles
    @property
    def primary_profile(self):
        return self.profiles[0]


# --- resolver unit tests --------------------------------------------------- #

def test_resolves_to_table_holding_the_column():
    s = _Sess([_p("orders", ["amount", "seg"]), _p("clubs", ["own_goals", "name"])])
    assert _resolve_tool_table(s, {"column": "own_goals"}).table_name == "clubs"   # not primary!
    assert _resolve_tool_table(s, {"column": "amount"}).table_name == "orders"     # primary


def test_no_column_referenced_falls_back_to_primary():
    s = _Sess([_p("orders", ["amount"]), _p("clubs", ["own_goals"])])
    assert _resolve_tool_table(s, {}).table_name == "orders"
    assert _resolve_tool_table(s, {"bins": 20}).table_name == "orders"  # non-column arg


def test_explicit_table_override():
    s = _Sess([_p("orders", ["amount"]), _p("clubs", ["own_goals"])])
    assert _resolve_tool_table(s, {"table": "clubs", "column": "own_goals"}).table_name == "clubs"
    with pytest.raises(ToolError):
        _resolve_tool_table(s, {"table": "nope"})


def test_columns_spanning_tables_is_a_clear_error():
    s = _Sess([_p("orders", ["amount"]), _p("clubs", ["own_goals"])])
    with pytest.raises(ToolError):
        _resolve_tool_table(s, {"value": "amount", "group": "own_goals"})


# --- integration: a tool actually runs on a non-primary table -------------- #

def test_tool_runs_on_non_primary_table(exhibit_home, sample_csv, tmp_path):
    import csv
    other = tmp_path / "clubs.csv"
    with open(other, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["own_goals", "name"])
        for i in range(8):
            w.writerow([i, f"c{i}"])
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, [sample_csv, other], MockLLM())
    # own_goals is only on the second (non-primary) table — before, this errored
    node = orchestrator.run_tool(session, "describe", {"column": "own_goals"})
    assert isinstance(node.payload, ToolResultPayload), \
        f"expected a result, got {node.kind.value}: {getattr(node,'error',None)}"
    session.close()
