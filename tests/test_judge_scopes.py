"""Slice 2 — graph-aware judge context + subgraph selectors."""

from exhibit.config import AppPaths
from exhibit.engine import graph, judge, orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import CritiquePayload, HypothesisPayload
from exhibit.store import db
from exhibit.store import nodes as node_store


def _session(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    return orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())


def test_graph_digest_has_structure_sections(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")
    digest = graph.build_graph_digest(session.conn, session.investigation.id)
    assert "CLAIMS & THEIR EVIDENCE:" in digest
    # the conclusion's supporting evidence is named, not just chronology
    assert "supported by:" in digest
    session.close()


def test_assemble_judge_input_is_sectioned(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")
    ctx = judge.assemble_judge_input(session.conn, session.investigation.id,
                                     directive="COMPARE THINGS")
    assert "GRAPH STRUCTURE" in ctx
    assert "FOCUS FOR THIS REVIEW:\nCOMPARE THINGS" in ctx
    assert "CHRONOLOGICAL TRANSCRIPT" in ctx
    # structure comes before the transcript (relationships lead)
    assert ctx.index("GRAPH STRUCTURE") < ctx.index("CHRONOLOGICAL TRANSCRIPT")
    session.close()


def test_scope_selectors(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    r1 = orchestrator.run_question(session, "monthly revenue trend")
    r2 = orchestrator.run_question(session, "revenue by region",
                                   parent_id=r1.conclusion_node.id)

    anc = graph.ancestry_ids(conn, inv, r2.conclusion_node.id)
    assert r2.conclusion_node.id in anc and r1.conclusion_node.id in anc

    desc = graph.descendant_ids(conn, inv, r1.conclusion_node.id)
    assert r2.conclusion_node.id in desc  # r2 forked under r1's conclusion
    session.close()


def test_judge_branches_spans_both_branches(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    conn, inv = session.conn, session.investigation.id
    r1 = orchestrator.run_question(session, "monthly revenue trend")
    a = orchestrator.run_question(session, "revenue by region", parent_id=r1.conclusion_node.id)
    b = orchestrator.run_question(session, "revenue by segment", parent_id=r1.conclusion_node.id)

    scope = (graph.ancestry_ids(conn, inv, a.conclusion_node.id)
             | graph.ancestry_ids(conn, inv, b.conclusion_node.id))
    assert a.conclusion_node.id in scope and b.conclusion_node.id in scope
    assert r1.conclusion_node.id in scope  # shared root visible to the comparison

    node = orchestrator.judge_branches(session, a.conclusion_node.id, b.conclusion_node.id)
    assert isinstance(node.payload, CritiquePayload) and node.payload.mode == "review"
    session.close()


def test_judge_unresolved_runs_over_open_hypotheses(exhibit_home, sample_csv):
    session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")
    orchestrator.judge_investigation(session)  # materializes an untested alternative
    assert graph.unresolved_hypotheses(session.conn, session.investigation.id)
    node = orchestrator.judge_unresolved(session)
    assert isinstance(node.payload, CritiquePayload) and node.payload.mode == "review"
    session.close()
