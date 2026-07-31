import pytest

from exhibit.data import loader
from exhibit.data import profile as profiler
from exhibit.tools import REGISTRY, get_tool, tool_specs
from exhibit.tools.base import ToolError


@pytest.fixture
def loaded(tmp_path, sample_csv):
    duckdb_path = tmp_path / "data.duckdb"
    _, table_name, schema = loader.load(sample_csv, duckdb_path)
    con = loader.open_readonly(duckdb_path)
    profile = profiler.profile_dataset(con, table_name, schema)
    return con, table_name, profile


def _run(name, loaded, **inputs):
    con, table_name, profile = loaded
    tool = get_tool(name)
    return tool.run(con, table_name, profile, tool.parse_inputs(inputs))


def test_all_new_tools_registered():
    names = {s["name"] for s in tool_specs()}
    expected = {"t_test", "mann_whitney", "chi_square", "correlation", "correlation_matrix",
                "moving_average", "growth_rates", "trend", "linear_fit", "logistic_fit",
                "cohort_retention", "funnel", "rfm"}
    assert expected <= names


# --- hypothesis / correlation --------------------------------------------- #

def test_t_test(loaded):
    r = _run("t_test", loaded, value="revenue", group="segment")
    assert "p_value" in r.metrics and "t_stat" in r.metrics
    assert r.metrics["n_a"] > 0 and r.metrics["n_b"] > 0


def test_mann_whitney(loaded):
    r = _run("mann_whitney", loaded, value="revenue", group="segment")
    assert 0.0 <= r.metrics["p_value"] <= 1.0


def test_chi_square(loaded):
    r = _run("chi_square", loaded, column_a="segment", column_b="region")
    assert r.metrics["dof"] > 0 and 0.0 <= r.metrics["p_value"] <= 1.0
    assert 0.0 <= r.metrics["cramers_v"] <= 1.0


def test_correlation(loaded):
    r = _run("correlation", loaded, x="quantity", y="revenue", method="pearson")
    assert -1.0 <= r.metrics["r"] <= 1.0 and r.metrics["n"] == 247


def test_correlation_matrix(loaded):
    r = _run("correlation_matrix", loaded)
    assert r.table is not None and r.metrics["columns"] >= 2


# --- time series ----------------------------------------------------------- #

def test_moving_average(loaded):
    r = _run("moving_average", loaded, date_column="order_date", measure="revenue", window=3)
    assert r.metrics["periods"] == 8 and "moving_avg" in r.table.columns


def test_growth_rates(loaded):
    r = _run("growth_rates", loaded, date_column="order_date", measure="revenue", periods_ago=1)
    assert "pct_change" in r.table.columns and r.metrics["periods"] == 8


def test_trend(loaded):
    r = _run("trend", loaded, date_column="order_date", measure="revenue")
    assert "slope_per_period" in r.metrics and r.metrics["direction"] in {"rising", "falling", "flat"}


# --- modeling -------------------------------------------------------------- #

def test_linear_fit(loaded):
    r = _run("linear_fit", loaded, target="revenue", predictors=["quantity", "unit_price"])
    assert 0.0 <= r.metrics["r_squared"] <= 1.0
    assert set(r.metrics["coefficients"]) == {"intercept", "quantity", "unit_price"}


def test_logistic_fit_needs_binary_target(loaded):
    with pytest.raises(ToolError):
        _run("logistic_fit", loaded, target="revenue", predictors=["quantity"])


def test_logistic_fit_on_binary(tmp_path):
    # synthetic separable-ish binary target
    csv = tmp_path / "bin.csv"
    rows = ["x,y"] + [f"{i},{1 if i > 50 else 0}" for i in range(100)]
    csv.write_text("\n".join(rows) + "\n")
    dp = tmp_path / "d.duckdb"
    _, tn, sch = loader.load(csv, dp)
    con = loader.open_readonly(dp)
    prof = profiler.profile_dataset(con, tn, sch)
    tool = get_tool("logistic_fit")
    r = tool.run(con, tn, prof, tool.parse_inputs({"target": "y", "predictors": ["x"]}))
    assert 0.0 <= r.metrics["accuracy"] <= 1.0 and r.metrics["accuracy"] > 0.8


# --- customer analytics ---------------------------------------------------- #

def test_cohort_retention(loaded):
    r = _run("cohort_retention", loaded, entity="customer_id", date_column="order_date")
    assert r.metrics["cohorts"] >= 1 and "retention_pct" in r.table.columns


def test_rfm(loaded):
    r = _run("rfm", loaded, entity="customer_id", date_column="order_date", monetary="revenue")
    assert r.metrics["entities"] > 0 and "rfm_score" in r.table.columns


def test_funnel(tmp_path):
    csv = tmp_path / "events.csv"
    rows = ["user_id,stage"]
    # 10 view, 6 cart, 3 purchase
    for i in range(10):
        rows.append(f"u{i},view")
    for i in range(6):
        rows.append(f"u{i},cart")
    for i in range(3):
        rows.append(f"u{i},purchase")
    csv.write_text("\n".join(rows) + "\n")
    dp = tmp_path / "d.duckdb"
    _, tn, sch = loader.load(csv, dp)
    con = loader.open_readonly(dp)
    prof = profiler.profile_dataset(con, tn, sch)
    tool = get_tool("funnel")
    r = tool.run(con, tn, prof, tool.parse_inputs(
        {"entity": "user_id", "stage_column": "stage", "steps": ["view", "cart", "purchase"]}))
    assert r.metrics["start_entities"] == 10 and r.metrics["end_entities"] == 3
    assert r.metrics["overall_conversion_pct"] == 30.0
