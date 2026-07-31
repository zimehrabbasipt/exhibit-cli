from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import NodeKind
from exhibit.store import db
from exhibit.store import investigations as inv_store
from exhibit.store import nodes as node_store


def _kinds(conn, inv_id):
    return [n.kind for n in node_store.list_by_investigation(conn, inv_id)]


def test_end_to_end_and_resume(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)

    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM(), name="demo")
    inv_id = session.investigation.id

    # profile node exists immediately
    assert NodeKind.dataset_profile in _kinds(conn, inv_id)

    result = orchestrator.run_question(session, "Why did revenue decline in June?")

    assert not result.error_nodes
    assert result.plan_node is not None
    assert result.table_nodes
    assert result.conclusion_node is not None
    assert result.follow_up_nodes  # suggestions produced

    kinds = _kinds(conn, inv_id)
    for expected in (
        NodeKind.user_question,
        NodeKind.plan,
        NodeKind.sql_query,
        NodeKind.table_result,
        NodeKind.interpretation,
        NodeKind.conclusion,
        NodeKind.follow_up,
    ):
        assert expected in kinds, f"missing {expected}"

    # the mock's monthly query should find June as the low month
    table = result.table_nodes[0].payload.table
    assert table.row_count == 8  # Jan..Aug
    session.close()

    # --- resume ---
    conn2 = db.connect(paths.db_path)
    investigation = inv_store.resolve(conn2, inv_id)
    assert investigation is not None
    session2 = orchestrator.open_session(conn2, paths, investigation, MockLLM())
    assert session2.primary_profile.row_count == 247
    assert _kinds(conn2, inv_id) == kinds  # log intact after reopen
    session2.close()


class _DirectAnswerLLM:
    """Stub: returns a zero-step plan (profile answers it) and narrates directly."""

    name = "stub"

    def plan(self, question, profiles, context=""):
        from exhibit.models import Plan

        return Plan(question=question, rationale="Answerable from the profile.", steps=[])

    def generate_sql(self, step, profiles):  # pragma: no cover - never called
        raise AssertionError("SQL generation should not run for a zero-step plan")

    def narrate(self, question, profiles, outcomes, context=""):
        from exhibit.models import Conclusion, FollowUp, Interpretation

        assert outcomes == []  # narrates from the profile, no query results
        cols = len(profiles[0].columns)
        return (
            Interpretation(findings=[f"All {cols} columns are 0% null."]),
            Conclusion(summary="The dataset is fully complete.", confidence="high"),
            [FollowUp(question="Any duplicate order_ids?", why="uniqueness check")],
        )


def test_zero_step_plan_answers_from_profile(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, _DirectAnswerLLM())

    result = orchestrator.run_question(session, "Are there any null values?")

    # No SQL/table nodes were produced, but we still get a grounded conclusion
    kinds = _kinds(conn, session.investigation.id)
    assert NodeKind.sql_query not in kinds
    assert NodeKind.table_result not in kinds
    assert result.plan_node is not None and not result.plan_node.payload.plan.steps
    assert result.conclusion_node is not None
    assert "complete" in result.conclusion_node.payload.conclusion.summary
    assert not result.error_nodes
    session.close()


class _ToolSelectingLLM:
    """Stub: plans one tool step (decompose_contribution) and fills real args."""

    name = "stub"

    def plan(self, question, profiles, context=""):
        from exhibit.models import Plan, PlanStep

        return Plan(
            question=question,
            rationale="Decompose the change by segment.",
            steps=[PlanStep(id="s1", intent="decompose", description="which segment drove it",
                            method="tool", tool="decompose_contribution")],
        )

    def generate_sql(self, step, profiles):  # pragma: no cover
        raise AssertionError("tool step must not generate SQL")

    def generate_tool_call(self, step, profile, tool):
        from exhibit.models import ToolCall

        return ToolCall(tool="decompose_contribution", inputs={
            "dimension": "segment", "measure": "revenue", "date_column": "order_date",
            "period_a": "2024-05", "period_b": "2024-06",
        })

    def narrate(self, question, profiles, outcomes, context=""):
        from exhibit.models import Conclusion, Interpretation

        assert len(outcomes) == 1 and outcomes[0].tool_result is not None
        tr = outcomes[0].tool_result
        return (
            Interpretation(findings=[tr.summary], evidence_node_ids=[outcomes[0].node_id]),
            Conclusion(summary=tr.summary, confidence="high",
                       evidence_node_ids=[outcomes[0].node_id]),
            [],
        )


def test_llm_selected_tool_runs_and_narrates(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, _ToolSelectingLLM())

    result = orchestrator.run_question(session, "which segment drove the June decline?")

    kinds = _kinds(conn, session.investigation.id)
    assert NodeKind.tool_call in kinds and NodeKind.tool_result in kinds
    assert NodeKind.sql_query not in kinds
    assert result.tool_nodes and not result.error_nodes
    # the tool actually computed the contribution and it flowed into the conclusion
    assert "Corporate" in result.conclusion_node.payload.conclusion.summary
    session.close()


class _TwoSqlSteps:
    """Plans two SQL steps and records the prior_outcomes each step's SQL-gen saw."""

    name = "stub"

    def __init__(self):
        self.prior_seen = []

    def plan(self, question, profiles, context=""):
        from exhibit.models import Plan, PlanStep

        return Plan(question=question, rationale="two dependent steps", steps=[
            PlanStep(id="s1", intent="find_set", description="find a set"),
            PlanStep(id="s2", intent="use_set", description="analyze that set"),
        ])

    def generate_sql(self, step, profiles, prior_outcomes=()):
        from exhibit.models import SqlQuery

        self.prior_seen.append((step.id, [o.sql for o in prior_outcomes]))
        return SqlQuery(step_id=step.id, sql="SELECT 1 AS x")

    def generate_tool_call(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def narrate(self, question, profiles, outcomes, context=""):
        from exhibit.models import Conclusion, Interpretation

        return Interpretation(findings=[]), Conclusion(summary="ok"), []


def test_step2_receives_step1_outcome_with_sql(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = _TwoSqlSteps()
    session = orchestrator.start_investigation(conn, paths, sample_csv, client)

    orchestrator.run_question(session, "dependent-step question")

    # step 1 saw no prior; step 2 saw exactly step 1's (guarded) SQL
    assert client.prior_seen[0] == ("s1", [])
    step2_id, step2_prior = client.prior_seen[1]
    assert step2_id == "s2"
    assert len(step2_prior) == 1 and step2_prior[0] and "limit" in step2_prior[0].lower()
    session.close()


class _FastStub:
    """A fast-path-capable stub: its investigate() runs one SQL query then answers."""

    name = "faststub"
    supports_fast_path = True

    def plan(self, *a, **k):  # pragma: no cover - fast path shouldn't plan
        raise AssertionError("fast path must not call plan()")

    def investigate(self, question, profiles, context, run_sql):
        out = run_sql(f'SELECT COUNT(*) AS n FROM "{profiles[0].table_name}"')
        return f"The dataset has rows. Query returned: {out[:40]}"


def test_fast_path_is_opt_in(exhibit_home, sample_csv):
    from exhibit.engine.orchestrator import _should_use_fast_path

    client = _FastStub()
    # full is the default; fast only when explicitly requested AND supported
    assert _should_use_fast_path(client, False) is False    # default → full
    assert _should_use_fast_path(client, True) is True       # /quick → fast
    assert _should_use_fast_path(MockLLM(), True) is False   # mock can't fast-path


def test_fast_path_nodes_when_requested(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = _FastStub()
    session = orchestrator.start_investigation(conn, paths, sample_csv, client)

    result = orchestrator.run_question(session, "how many rows are there?", fast=True)
    assert result.path == "fast"
    kinds = _kinds(conn, session.investigation.id)
    assert NodeKind.plan not in kinds            # fast path skips the plan node
    assert NodeKind.sql_query in kinds           # but still records the SQL it ran
    assert NodeKind.table_result in kinds
    assert result.conclusion_node is not None    # and a conclusion from the final answer
    session.close()


def test_progress_events_are_emitted(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())

    events = []

    class _Recorder:
        def start_planning(self): events.append("plan_start")
        def plan_ready(self, plan): events.append(("plan_ready", len(plan.steps)))
        def step_start(self, i, step): events.append(("step_start", i))
        def step_done(self, i, step, ok): events.append(("step_done", i, ok))
        def start_narrating(self): events.append("narrate")

    orchestrator.run_question(session, "monthly revenue trend", progress=_Recorder())

    assert events[0] == "plan_start"
    assert ("plan_ready", 1) in events           # mock plans one step
    assert ("step_start", 0) in events
    assert ("step_done", 0, True) in events
    assert "narrate" in events
    session.close()


def test_batch_mode_executes_inline_sql_without_extra_calls(exhibit_home, sample_csv):
    """A plan whose steps carry inline SQL is executed directly — generate_sql
    must not be called — and the plan node persists the command for re-run."""
    class _BatchStub:
        name = "batch"
        supports_fast_path = False

        def plan(self, question, profiles, context=""):
            from exhibit.models import Plan, PlanStep
            t = profiles[0].table_name
            return Plan(question=question, rationale="inline", steps=[
                PlanStep(id="s1", intent="count", description="row count",
                         method="sql", sql=f'SELECT COUNT(*) AS n FROM "{t}"'),
            ])

        def generate_sql(self, *a, **k):  # must not be called in batch mode
            raise AssertionError("batch mode must not call generate_sql")

        def narrate(self, question, profiles, outcomes, context=""):
            from exhibit.models import Conclusion, Interpretation
            return Interpretation(findings=[]), Conclusion(summary="ok"), []

    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, _BatchStub())
    result = orchestrator.run_question(session, "how many rows?")

    assert result.plan_node is not None
    assert result.plan_node.payload.plan.steps[0].sql  # command saved in the tree
    assert result.table_nodes and not result.error_nodes

    # the saved task list re-runs deterministically
    rerun = orchestrator.rerun_plan(session, result.plan_node)
    assert rerun.table_nodes and not rerun.error_nodes
    session.close()


def test_rerun_is_deterministic(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    result = orchestrator.run_question(session, "monthly revenue trend")

    sql_node = next(
        n
        for n in node_store.list_by_investigation(conn, session.investigation.id)
        if n.kind == NodeKind.sql_query
    )
    original = result.table_nodes[0].payload.table
    rerun = orchestrator.rerun_sql_node(session, sql_node)
    assert rerun.payload.table.rows_preview == original.rows_preview
    session.close()
