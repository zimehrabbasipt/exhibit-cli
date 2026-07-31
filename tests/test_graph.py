"""Slice 1 — typed edge DAG: structural edges captured on conclusions, hypothesis
status as a projection over edges, and the deterministic graph checks."""

from exhibit.config import AppPaths
from exhibit.engine import graph, orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import (
    EdgeCreatedBy,
    EdgeStatus,
    EdgeType,
    HypothesisPayload,
    NodeKind,
)
from exhibit.store import db
from exhibit.store import edges as edge_store
from exhibit.store import nodes as node_store


def _session(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    return orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())


def test_supports_edges_created_on_conclusion(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    result = orchestrator.run_question(session, "monthly revenue trend")
    edges = edge_store.list_by_investigation(session.conn, session.investigation.id)
    supports = [e for e in edges if e.relationship == EdgeType.supports]
    assert supports, "conclusion should have engine-captured supports edges"
    for e in supports:
        assert e.created_by == EdgeCreatedBy.engine
        assert e.status == EdgeStatus.active
        assert e.target_id == result.conclusion_node.id
    session.close()


def test_hypothesis_status_projection(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    hyp = node_store.append(conn, inv, HypothesisPayload(statement="Manager drove it"),
                            title="h", parent_id=None)
    ev = node_store.append(conn, inv, HypothesisPayload(statement="ev-standin"),
                           title="e", parent_id=None)  # any node works as an edge source

    assert graph.hypothesis_status(conn, hyp.id) == "proposed"

    edge_store.add_edge(conn, inv, ev.id, hyp.id, EdgeType.tests, EdgeCreatedBy.engine)
    assert graph.hypothesis_status(conn, hyp.id) == "unresolved"

    edge_store.add_edge(conn, inv, ev.id, hyp.id, EdgeType.supports, EdgeCreatedBy.engine)
    assert graph.hypothesis_status(conn, hyp.id) == "supported"

    edge_store.add_edge(conn, inv, ev.id, hyp.id, EdgeType.contradicts, EdgeCreatedBy.judge)
    assert graph.hypothesis_status(conn, hyp.id) == "weakened"
    session.close()


def test_proposed_edges_do_not_flip_status(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    hyp = node_store.append(conn, inv, HypothesisPayload(statement="H"), title="h", parent_id=None)
    src = node_store.append(conn, inv, HypothesisPayload(statement="s"), title="s", parent_id=None)
    # a *proposed* supports edge must not count until accepted
    edge_store.add_edge(conn, inv, src.id, hyp.id, EdgeType.supports,
                        EdgeCreatedBy.judge, status=EdgeStatus.proposed)
    assert graph.hypothesis_status(conn, hyp.id) == "proposed"
    session.close()


def test_graph_lint_untested_hypothesis(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    node_store.append(conn, inv, HypothesisPayload(statement="never tested"),
                      title="h", parent_id=None)
    checks = graph.graph_lint(conn, inv)
    assert any(c.name == "untested_hypothesis" and c.status == "warn" for c in checks)
    session.close()


def test_graph_lint_shared_evidence_across_branches(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    r1 = orchestrator.run_question(session, "monthly revenue trend")
    # two sibling branches both forked off r1's conclusion — neither is an ancestor
    # of the other, so they are divergent
    r2 = orchestrator.run_question(session, "revenue by region",
                                   parent_id=r1.conclusion_node.id)
    r3 = orchestrator.run_question(session, "revenue by segment",
                                   parent_id=r1.conclusion_node.id)
    # make one evidence node support BOTH sibling branches' conclusions
    ev = [e for e in edge_store.list_by_investigation(conn, inv)
          if e.relationship == EdgeType.supports and e.target_id == r2.conclusion_node.id][0]
    edge_store.add_edge(conn, inv, ev.source_id, r3.conclusion_node.id,
                        EdgeType.supports, EdgeCreatedBy.engine)
    checks = graph.graph_lint(conn, inv)
    assert any(c.name == "shared_evidence" for c in checks)
    session.close()


def test_statements_similar_dedup():
    # the real near-duplicate pairs from a football report
    sched_a = "Schedule congestion / fixture pile-up eroded home advantage by increasing fatigue and rotation."
    sched_b = "Schedule congestion / fixture pile-up eroded home advantage via fatigue and rotation, coinciding with (but distinct from) empty stadiums."
    comp_a = "Compositional shift — the set of leagues/matches contributing to each period differs — creates a spurious aggregate move."
    comp_b = "Compositional shift — the leagues/matches in the 'crowd' bucket differ from those in the 'empty' bucket — creates a spurious gradient."
    assert graph.statements_similar(sched_a, sched_b)
    assert graph.statements_similar(comp_a, comp_b)
    # distinct alternatives must NOT be merged
    referee = "Referee behavior change (fewer fan-influenced decisions) is the mechanism."
    reverse = "Reverse causality on cards: away teams performing better forces trailing home teams into more fouls."
    assert not graph.statements_similar(sched_a, referee)
    assert not graph.statements_similar(comp_a, reverse)
    assert not graph.statements_similar(referee, reverse)


def test_judge_materializes_hypotheses(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")
    orchestrator.judge_investigation(session)  # MockLLM returns 1 untested alternative
    nodes = node_store.list_by_investigation(session.conn, session.investigation.id)
    hyps = [n for n in nodes if isinstance(n.payload, HypothesisPayload)]
    assert hyps, "judge should materialize untested alternatives as hypothesis nodes"
    assert hyps[0].payload.origin == "judge"
    # the alternative_to edge is a semantic assertion -> lands as proposed
    edges = [e for e in edge_store.list_by_investigation(session.conn, session.investigation.id)
             if e.relationship == EdgeType.alternative_to]
    assert edges and edges[0].status == EdgeStatus.proposed
    assert edges[0].created_by == EdgeCreatedBy.judge
    session.close()
