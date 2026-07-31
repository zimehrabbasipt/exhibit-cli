"""Branching: a question can fork off an earlier node instead of threading under
the latest conclusion, and its context is scoped to that node's ancestry."""

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.engine.context import build_context
from exhibit.llm.mock import MockLLM
from exhibit.store import db


def test_branch_parents_off_chosen_node(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())

    r1 = orchestrator.run_question(session, "monthly revenue trend")
    # a normal follow-up threads under r1's conclusion
    r2 = orchestrator.run_question(session, "revenue by region")
    assert r2.question_node.parent_id == r1.conclusion_node.id

    # branch off r1's conclusion instead of the latest turn
    r3 = orchestrator.run_question(
        session, "what about revenue by segment", parent_id=r1.conclusion_node.id
    )
    assert r3.question_node.parent_id == r1.conclusion_node.id
    # it is a real sibling of r2 (same parent), not threaded under r2
    assert r3.question_node.parent_id != r2.conclusion_node.id
    session.close()


def test_branch_context_scoped_to_ancestry(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    inv = session.investigation.id

    r1 = orchestrator.run_question(session, "monthly revenue trend")
    orchestrator.run_question(session, "revenue by region")  # r2 — a sibling to ignore

    # Global (linear) context sees the most recent turn (r2).
    full = build_context(conn, inv)
    assert "revenue by region" in full

    # Context scoped to r1's ancestry sees r1 but is blind to its sibling r2.
    scoped = build_context(conn, inv, from_node_id=r1.conclusion_node.id)
    assert "monthly revenue trend" in scoped
    assert "revenue by region" not in scoped
    session.close()
