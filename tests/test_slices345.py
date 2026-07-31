"""depends_on edges (planner-declared), the metrics semantic layer, and atomic claims."""

from exhibit.config import AppPaths
from exhibit.engine import graph, orchestrator
from exhibit.engine.context import build_context
from exhibit.llm.mock import MockLLM
from exhibit.models import (
    Claim,
    EdgeCreatedBy,
    EdgeType,
    JudgeReview,
    MetricPayload,
    NodeKind,
    PlanStep,
)
from exhibit.store import db
from exhibit.store import edges as edge_store
from exhibit.store import nodes as node_store


def _session(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    return orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())


# --- A. depends_on ---------------------------------------------------------- #

def test_planstep_carries_depends_on():
    s = PlanStep(id="s2", intent="x", description="y", depends_on=["s1"])
    assert s.depends_on == ["s1"]


def test_capture_dependencies_creates_edges(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    # two stand-in result nodes and a planner-declared dependency s2 -> s1
    n1 = node_store.append(conn, inv, MetricPayload(name="a", sql="1"), title="a", parent_id=None)
    n2 = node_store.append(conn, inv, MetricPayload(name="b", sql="2"), title="b", parent_id=None)
    steps = [PlanStep(id="s1", intent="base", description="d"),
             PlanStep(id="s2", intent="dep", description="d", depends_on=["s1"])]
    orchestrator._capture_dependencies(session, steps, {"s1": n1.id, "s2": n2.id})
    deps = [e for e in edge_store.list_by_investigation(conn, inv)
            if e.relationship == EdgeType.depends_on]
    assert len(deps) == 1
    assert deps[0].source_id == n2.id and deps[0].target_id == n1.id
    assert deps[0].created_by == EdgeCreatedBy.narrator
    session.close()


# --- B. metrics semantic layer --------------------------------------------- #

def test_defined_metric_enters_context(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    orchestrator.run_question(session, "monthly revenue trend")  # so context is non-empty
    orchestrator.define_metric(session, "squad_value",
                               "SUM(market_value) FILTER (WHERE active)", "first-team only")
    ctx = build_context(conn, inv)
    assert "Defined metrics" in ctx
    assert "squad_value" in ctx and "first-team only" in ctx
    session.close()


def test_metric_drift_check(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    orchestrator.define_metric(session, "squad_value", "SUM(all_players)")
    orchestrator.define_metric(session, "squad_value", "SUM(registered_squad)")  # different!
    checks = graph.graph_lint(conn, inv)
    assert any(c.name == "metric_drift" and c.status == "warn" for c in checks)
    session.close()


# --- C. atomic claims ------------------------------------------------------- #

def test_judge_review_carries_claims(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")
    node = orchestrator.judge_investigation(session)
    review = node.payload.review
    assert review.claims, "the judge should decompose the conclusion into claims"
    c = review.claims[0]
    assert c.claim_type in ("descriptive", "comparative", "causal", "forecast")
    assert c.verdict in ("supported", "weak", "unsupported")
    session.close()
