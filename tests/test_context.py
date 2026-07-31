from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.engine.context import build_context
from exhibit.llm.mock import MockLLM
from exhibit.models import NodeKind
from exhibit.store import db
from exhibit.store import nodes as node_store


def _session(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    return conn, orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())


def test_context_empty_before_any_question(exhibit_home, sample_csv):
    conn, session = _session(exhibit_home, sample_csv)
    # only the dataset_profile node exists → no prior Q&A
    assert build_context(conn, session.investigation.id) == ""
    session.close()


def test_context_includes_prior_qa_and_results(exhibit_home, sample_csv):
    conn, session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")

    ctx = build_context(conn, session.investigation.id)
    assert "monthly revenue trend" in ctx          # prior question
    assert "Results already computed" in ctx        # recent result section
    assert "total_revenue" in ctx or "month" in ctx  # the actual result columns
    session.close()


def test_followup_question_threads_under_prior_conclusion(exhibit_home, sample_csv):
    conn, session = _session(exhibit_home, sample_csv)
    r1 = orchestrator.run_question(session, "monthly revenue trend")
    r2 = orchestrator.run_question(session, "what about by region?")

    # the second question's parent is the first question's conclusion node
    assert r1.conclusion_node is not None
    assert r2.question_node.parent_id == r1.conclusion_node.id
    session.close()


def test_rolling_summary_compacts_old_turns(exhibit_home, sample_csv):
    from exhibit.models import NodeKind

    conn, session = _session(exhibit_home, sample_csv)
    # 4 turns → the oldest (turn 1) ages out of the 3-turn verbatim window
    for i in range(4):
        orchestrator.run_question(session, f"question number {i}")

    nodes = node_store.list_by_investigation(conn, session.investigation.id)
    summaries = [n for n in nodes if n.kind == NodeKind.summary]
    assert summaries, "a rolling summary node should be created after eviction"
    assert summaries[-1].payload.text  # mock produced some summary text

    ctx = build_context(conn, session.investigation.id)
    assert "Summary of earlier turns" in ctx      # summary section present
    assert "Most recent turns (verbatim)" in ctx  # recent turns still verbatim
    session.close()


def test_no_summary_before_window_fills(exhibit_home, sample_csv):
    from exhibit.models import NodeKind

    conn, session = _session(exhibit_home, sample_csv)
    for i in range(2):  # only 2 turns → nothing aged out yet
        orchestrator.run_question(session, f"q{i}")
    nodes = node_store.list_by_investigation(conn, session.investigation.id)
    assert not [n for n in nodes if n.kind == NodeKind.summary]
    session.close()


def test_context_passed_into_planner(exhibit_home, sample_csv):
    """The orchestrator must hand the built context to the planner on turn 2."""
    conn, session = _session(exhibit_home, sample_csv)
    orchestrator.run_question(session, "monthly revenue trend")

    seen = {}

    class _Spy(MockLLM):
        def plan(self, question, profile, context=""):
            seen["context"] = context
            return super().plan(question, profile, context)

    session.client = _Spy()
    orchestrator.run_question(session, "and by segment?")
    assert "monthly revenue trend" in seen["context"]  # prior turn reached the planner
    session.close()
