from exhibit.config import AppPaths
from exhibit.engine import charts, orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import ChartPayload, NodeKind, ResultTable
from exhibit.store import db
from exhibit.store import nodes as node_store


def test_suggest_chart_picks_line_for_temporal():
    t = ResultTable(columns=["month", "total"],
                    rows_preview=[["2024-01-01", 100.0], ["2024-02-01", 120.0]], row_count=2)
    spec = charts.suggest_chart(t)
    assert spec and spec["chart_type"] == "line" and spec["x"] == "month" and spec["y"] == "total"


def test_suggest_chart_picks_bar_for_categorical():
    t = ResultTable(columns=["segment", "revenue"],
                    rows_preview=[["A", 10.0], ["B", 20.0], ["C", 5.0]], row_count=3)
    spec = charts.suggest_chart(t)
    assert spec and spec["chart_type"] == "bar"


def test_suggest_chart_none_for_single_row():
    t = ResultTable(columns=["n"], rows_preview=[[247]], row_count=1)
    assert charts.suggest_chart(t) is None


def test_render_png_writes_file(tmp_path):
    t = ResultTable(columns=["month", "total"],
                    rows_preview=[["2024-01", 100.0], ["2024-02", 140.0], ["2024-03", 90.0]],
                    row_count=3)
    out = tmp_path / "c.png"
    charts.render_png(t, "line", "month", "total", "Total by month", out)
    assert out.exists() and out.stat().st_size > 1000  # a real PNG, not empty


def test_render_png_bar_and_histogram(tmp_path):
    t = ResultTable(columns=["seg", "rev"], rows_preview=[["A", 10.0], ["B", 20.0]], row_count=2)
    charts.render_png(t, "bar", "seg", "rev", "Rev by seg", tmp_path / "bar.png")
    h = ResultTable(columns=["v"], rows_preview=[[float(i)] for i in range(30)], row_count=30)
    charts.render_png(h, "histogram", "v", None, "Distribution of v", tmp_path / "hist.png")
    assert (tmp_path / "bar.png").exists() and (tmp_path / "hist.png").exists()


def test_autochart_creates_chart_node_and_png(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    result = orchestrator.run_question(session, "monthly revenue trend")

    assert result.chart_nodes, "the headline monthly-trend result should be auto-charted"
    chart_node = result.chart_nodes[0]
    assert isinstance(chart_node.payload, ChartPayload)
    assert chart_node.artifact_path and chart_node.artifact_path.endswith(".png")
    from pathlib import Path
    assert Path(chart_node.artifact_path).exists()
    assert NodeKind.chart in [n.kind for n in node_store.list_by_investigation(conn, session.investigation.id)]
    session.close()
