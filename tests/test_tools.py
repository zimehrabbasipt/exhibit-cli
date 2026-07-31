import pytest

from exhibit.config import AppPaths
from exhibit.data import loader
from exhibit.data import profile as profiler
from exhibit.engine import orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import NodeKind
from exhibit.store import db
from exhibit.tools import REGISTRY, get_tool, tool_specs
from exhibit.tools.base import ToolError


@pytest.fixture
def loaded(tmp_path, sample_csv):
    """A read-only DuckDB connection + profile over the sample sales data."""
    duckdb_path = tmp_path / "data.duckdb"
    _, table_name, schema = loader.load(sample_csv, duckdb_path)
    con = loader.open_readonly(duckdb_path)
    profile = profiler.profile_dataset(con, table_name, schema)
    return con, table_name, profile


def _run(tool_name, loaded, **inputs):
    con, table_name, profile = loaded
    tool = get_tool(tool_name)
    parsed = tool.parse_inputs(inputs)
    return tool.run(con, table_name, profile, parsed)


# --- registry -------------------------------------------------------------- #

def test_registry_specs_have_schemas():
    specs = {s["name"]: s for s in tool_specs()}
    assert {"describe", "outliers", "decompose_contribution",
            "volume_vs_rate", "fit_distribution"} <= set(specs)
    for s in specs.values():
        assert "input_schema" in s and "properties" in s["input_schema"]


def test_unknown_tool_raises():
    with pytest.raises(KeyError):
        get_tool("does_not_exist")


# --- describe -------------------------------------------------------------- #

def test_describe_revenue(loaded):
    r = _run("describe", loaded, column="revenue")
    assert r.metrics["count"] == 247
    assert r.metrics["min"] <= r.metrics["median"] <= r.metrics["max"]
    assert r.metrics["stddev"] > 0


def test_describe_rejects_non_numeric(loaded):
    with pytest.raises(ToolError):
        _run("describe", loaded, column="segment")


def test_describe_rejects_missing_column(loaded):
    with pytest.raises(ToolError):
        _run("describe", loaded, column="nope")


# --- outliers -------------------------------------------------------------- #

def test_outliers_iqr(loaded):
    r = _run("outliers", loaded, column="revenue", method="iqr", threshold=1.5)
    assert r.metrics["lower_bound"] < r.metrics["upper_bound"]
    assert 0 <= r.metrics["outlier_fraction"] <= 1
    assert r.table is not None


# --- decompose_contribution ------------------------------------------------ #

def test_decompose_finds_june_drop(loaded):
    r = _run(
        "decompose_contribution", loaded,
        dimension="segment", measure="revenue",
        date_column="order_date", period_a="2024-05", period_b="2024-06",
    )
    # revenue fell sharply from May to June
    assert r.metrics["total_delta"] < 0
    # a top contributor is identified with a contribution percentage
    assert r.metrics["top_contributor"] in {"Consumer", "Corporate", "Home Office"}
    assert r.table.row_count == 3


def test_decompose_bad_period_format(loaded):
    with pytest.raises(ToolError):
        _run("decompose_contribution", loaded, dimension="segment", measure="revenue",
             date_column="order_date", period_a="June", period_b="2024-06")


# --- volume_vs_rate -------------------------------------------------------- #

def test_volume_vs_rate_decomposition_sums(loaded):
    r = _run("volume_vs_rate", loaded, measure="revenue",
             date_column="order_date", period_a="2024-05", period_b="2024-06")
    m = r.metrics
    # effects reconstruct the total change (identity), within rounding
    assert abs((m["volume_effect"] + m["rate_effect"] + m["interaction"]) - m["delta_total"]) < 1.0
    assert m["primary_driver"] in {"volume", "rate"}
    assert m["orders_b"] < m["orders_a"]  # June had fewer orders


# --- fit_distribution ------------------------------------------------------ #

def test_fit_gaussian(loaded):
    r = _run("fit_distribution", loaded, column="revenue", family="gaussian")
    assert r.metrics["family"] == "gaussian"
    assert "p_value" in r.metrics and "mu" in r.metrics


def test_fit_poisson_on_counts(loaded):
    r = _run("fit_distribution", loaded, column="quantity", family="poisson")
    assert r.metrics["family"] == "poisson"
    assert r.metrics["lambda"] > 0
    assert "dispersion_index" in r.metrics


# --- node wiring ----------------------------------------------------------- #

def test_run_tool_creates_call_and_result_nodes(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())

    node = orchestrator.run_tool(session, "describe", {"column": "revenue"})
    assert node.kind == NodeKind.tool_result
    parent = orchestrator.node_store.get(conn, node.parent_id)
    assert parent.kind == NodeKind.tool_call
    session.close()


def test_run_tool_bad_input_records_error_node(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())

    node = orchestrator.run_tool(session, "describe", {"column": "segment"})
    assert node.kind == NodeKind.error
    session.close()
