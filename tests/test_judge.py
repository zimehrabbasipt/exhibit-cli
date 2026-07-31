"""Investigation judge: the deterministic linter fires the right warnings, and the
on-demand LLM judge (mock) produces + persists a structured review node."""

from exhibit.config import AppPaths
from exhibit.engine import judge, orchestrator
from exhibit.engine.orchestrator import RunResult
from exhibit.llm.mock import MockLLM
from exhibit.models import (
    Conclusion,
    ConclusionPayload,
    CritiquePayload,
    NodeKind,
    Node,
    Plan,
    PlanPayload,
    PlanStep,
    ResultTable,
    TablePayload,
)
from exhibit.store import db
from exhibit.store import nodes as node_store


def _node(payload, seq=1, kind=None):
    return Node(id=f"n{seq}", investigation_id="inv", seq=seq, parent_id=None,
                kind=kind or payload.kind, created_at="t", title="", payload=payload)


def _plan(n_steps):
    steps = [PlanStep(id=f"s{i}", intent="x", description="y") for i in range(n_steps)]
    return _node(PlanPayload(plan=Plan(question="q", steps=steps, rationale="r")))


def _concl(summary, confidence="medium"):
    return _node(ConclusionPayload(conclusion=Conclusion(summary=summary, confidence=confidence)))


def _table(rows):
    t = ResultTable(columns=["a"], rows_preview=[[1]], row_count=rows)
    return _node(TablePayload(table=t))


def test_lint_flags_ungrounded_conclusion():
    r = RunResult(question_node=_node(_concl("x").payload))  # dummy
    r.plan_node = _plan(2)          # 2 steps planned
    r.conclusion_node = _concl("Revenue rose.")
    # no table_nodes / tool_nodes -> ungrounded
    payload = judge.lint_result(r)
    names = {c.name: c.status for c in payload.checks}
    assert names["groundedness"] == "warn"


def test_lint_flags_causal_without_test():
    r = RunResult(question_node=_node(_concl("x").payload))
    r.plan_node = _plan(1)
    r.table_nodes = [_table(10)]
    r.conclusion_node = _concl("The decline was caused by the June outage.")
    payload = judge.lint_result(r)
    warns = {c.name for c in payload.checks if c.status == "warn"}
    assert "causal_claim" in warns


def test_lint_flags_high_confidence_thin_evidence_and_truncation():
    r = RunResult(question_node=_node(_concl("x").payload))
    r.plan_node = _plan(1)
    r.table_nodes = [_table(1000)]  # hits the 1000-row cap -> truncation
    r.conclusion_node = _concl("Clear winner.", confidence="high")  # 1 result + high
    payload = judge.lint_result(r)
    warns = {c.name for c in payload.checks if c.status == "warn"}
    assert "truncation" in warns
    assert "confidence_calibration" in warns


def test_lint_clean_conclusion_has_no_warnings():
    r = RunResult(question_node=_node(_concl("x").payload))
    r.plan_node = _plan(1)
    r.table_nodes = [_table(12)]
    r.conclusion_node = _concl("Revenue in June was 931.", confidence="medium")
    payload = judge.lint_result(r)
    assert [c for c in payload.checks if c.status == "warn"] == []


def test_lint_node_persisted_after_run(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    result = orchestrator.run_question(session, "monthly revenue trend")
    assert result.lint_node is not None
    assert isinstance(result.lint_node.payload, CritiquePayload)
    assert result.lint_node.payload.mode == "lint"
    kinds = [n.kind for n in node_store.list_by_investigation(conn, session.investigation.id)]
    assert NodeKind.critique in kinds
    session.close()


def test_judge_investigation_persists_review(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    orchestrator.run_question(session, "monthly revenue trend")
    node = orchestrator.judge_investigation(session)
    assert isinstance(node.payload, CritiquePayload)
    assert node.payload.mode == "review"
    assert node.payload.review is not None
    assert node.payload.review.next_query.question  # non-empty
    session.close()
