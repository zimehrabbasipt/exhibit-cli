"""Orchestrator: the execution loop, and the persisted node graph it produces.

A ``Session`` bundles the SQLite connection (metadata), the read-only DuckDB
connection (user data), the resolved paths, the investigation, its profile, and
the LLM client. ``run_question`` walks the pipeline and appends a typed node for
every step, linked by ``parent_id`` so the investigation forms a tree:

    user_question
      └─ plan
           ├─ sql_query ─ table_result
           ├─ interpretation
           └─ conclusion ─ follow_up*
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import uuid4

import duckdb

from ..config import INLINE_ROW_THRESHOLD, PROMPT_VERSION, AppPaths
from ..data import loader
from ..data import profile as profiler
from ..llm.base import LLMClient
from ..models import (
    ChartPayload,
    Conclusion,
    ConclusionPayload,
    DatasetProfile,
    EdgeCreatedBy,
    EdgeStatus,
    EdgeType,
    ErrorPayload,
    FollowUpPayload,
    HypothesisPayload,
    Investigation,
    InterpretationPayload,
    MetricPayload,
    Node,
    NodeKind,
    NodeStatus,
    PlanPayload,
    PlanStep,
    ProfilePayload,
    QuestionPayload,
    SqlPayload,
    SqlQuery,
    StepOutcome,
    SummaryPayload,
    TablePayload,
    ToolCall,
    ToolCallPayload,
    ToolResultPayload,
)
from . import graph
from . import judge
from ..store import artifacts
from ..store import edges as edge_store
from ..store import investigations as inv_store
from ..store import nodes as node_store
from ..tools import get_tool
from ..tools.base import ToolError
from . import charts, executor, narrator, planner, sqlgen
from .context import VERBATIM_TURNS, build_context
from .progress import NullProgress, Progress
from .sqlguard import guard_sql


@dataclass
class Session:
    conn: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    paths: AppPaths
    investigation: Investigation
    profiles: List[DatasetProfile]  # one per loaded table; [0] is primary
    client: LLMClient

    @property
    def primary_profile(self) -> DatasetProfile:
        return self.profiles[0]

    @property
    def duckdb_path(self) -> Path:
        return self.paths.investigation_dir(self.investigation.id) / "data.duckdb"

    def close(self) -> None:
        try:
            self.duck.close()
        finally:
            self.conn.close()


@dataclass
class RunResult:
    """Nodes created by a single ``run_question`` call, for the CLI to render."""

    question_node: Node
    plan_node: Optional[Node] = None
    table_nodes: List[Node] = field(default_factory=list)
    tool_nodes: List[Node] = field(default_factory=list)
    chart_nodes: List[Node] = field(default_factory=list)
    interpretation_node: Optional[Node] = None
    conclusion_node: Optional[Node] = None
    follow_up_nodes: List[Node] = field(default_factory=list)
    lint_node: Optional[Node] = None  # deterministic critique of the conclusion
    error_nodes: List[Node] = field(default_factory=list)
    path: str = "full"  # "full" (plan→steps→narrate) or "fast" (agentic run_sql loop)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Investigation lifecycle
# --------------------------------------------------------------------------- #


def start_investigation(
    conn: sqlite3.Connection,
    paths: AppPaths,
    sources,  # Path | list[Path] | a "postgres://…" DSN string
    client: LLMClient,
    name: Optional[str] = None,
    pg_tables: Optional[List[Tuple[str, str]]] = None,   # [(schema, table), …] for a DSN source
    max_rows: Optional[int] = None,                      # per-table row cap on import
) -> Session:
    """Load one or more sources (each -> its own table), profile each, and persist the
    investigation with one dataset_profile node per table. A source may be local
    CSV/Parquet files or a Postgres DSN (selected tables snapshotted read-only)."""
    dsn = None
    if loader.is_postgres_dsn(sources):
        dsn = sources
    elif isinstance(sources, (list, tuple)) and len(sources) == 1 and loader.is_postgres_dsn(sources[0]):
        dsn = sources[0]

    investigation_id = uuid4().hex
    paths.ensure_investigation_dirs(investigation_id)
    duckdb_path = paths.investigation_dir(investigation_id) / "data.duckdb"
    loader.create_database(duckdb_path)

    existing: set = set()
    loaded = []  # (table_name, schema, fmt, source_display, source_row_count)

    if dsn:
        if not pg_tables:
            raise ValueError("select tables to snapshot from Postgres (pg_tables required)")
        src_display = loader.redact_dsn(dsn)
        for schema, table in pg_tables:
            local = re.sub(r"\W+", "_", table).strip("_").lower() or "table"
            while local in existing:
                local += "_x"
            existing.add(local)
            smap, source_rows, _imported = loader.add_postgres_table(
                duckdb_path, dsn, schema, table, local, max_rows)
            loaded.append((local, smap, "postgres", src_display, source_rows))
        default_name = src_display.rstrip("/").split("/")[-1] or "postgres"
    else:
        sources = loader.expand_sources(sources)
        if not sources:
            raise ValueError("no data files provided")
        for src in sources:
            fmt = loader.detect_format(src)
            table_name = loader.safe_table_name(src, existing)
            existing.add(table_name)
            schema = loader.add_table(duckdb_path, src, fmt, table_name)
            loaded.append((table_name, schema, fmt, str(src.expanduser().resolve()), None))
        default_name = sources[0].stem

    duck = loader.open_readonly(duckdb_path)
    profiles = []
    snapshot_at = _now()
    for tn, sc, fmt, disp, src_rows in loaded:
        pr = profiler.profile_dataset(duck, tn, sc)
        if fmt == "postgres":
            pr.source = disp
            pr.snapshot_at = snapshot_at
            pr.source_row_count = src_rows
        profiles.append(pr)

    primary_table, primary_schema, primary_fmt, primary_path = (
        loaded[0][0], loaded[0][1], loaded[0][2], loaded[0][3]
    )
    investigation = Investigation(
        id=investigation_id,
        name=name or default_name,
        created_at=_now(),
        data_path=primary_path,
        data_format=primary_fmt,  # type: ignore[arg-type]
        table_name=primary_table,
        schema_map=primary_schema,
    )
    inv_store.insert(conn, investigation)

    first_node_id = None
    for profile in profiles:
        node = node_store.append(
            conn,
            investigation_id,
            ProfilePayload(profile=profile),
            title=f"Table `{profile.table_name}`: {profile.row_count:,} rows, "
            f"{len(profile.columns)} columns",
        )
        first_node_id = first_node_id or node.id
    inv_store.set_profile_node(conn, investigation_id, first_node_id)
    investigation.profile_node_id = first_node_id

    return Session(
        conn=conn,
        duck=duck,
        paths=paths,
        investigation=investigation,
        profiles=profiles,
        client=client,
    )


def add_dataset(session: Session, source: Path) -> DatasetProfile:
    """Add another table to an existing investigation (via /add). Reopens the
    read-only connection around a brief read-write materialize."""
    source = Path(source)
    fmt = loader.detect_format(source)
    existing = {p.table_name for p in session.profiles}
    table_name = loader.safe_table_name(source, existing)

    # DuckDB single-writer: drop the read-only handle, write, reopen read-only.
    session.duck.close()
    schema = loader.add_table(session.duckdb_path, source, fmt, table_name)
    session.duck = loader.open_readonly(session.duckdb_path)

    profile = profiler.profile_dataset(session.duck, table_name, schema)
    node_store.append(
        session.conn,
        session.investigation.id,
        ProfilePayload(profile=profile),
        title=f"Table `{profile.table_name}`: {profile.row_count:,} rows, "
        f"{len(profile.columns)} columns",
    )
    session.profiles.append(profile)
    return profile


def open_session(
    conn: sqlite3.Connection,
    paths: AppPaths,
    investigation: Investigation,
    client: LLMClient,
) -> Session:
    """Reopen an existing investigation for resumption (all tables)."""
    duckdb_path = paths.investigation_dir(investigation.id) / "data.duckdb"
    duck = loader.open_readonly(duckdb_path)
    profiles = _load_profiles(conn, investigation)
    return Session(
        conn=conn,
        duck=duck,
        paths=paths,
        investigation=investigation,
        profiles=profiles,
        client=client,
    )


def _load_profiles(conn: sqlite3.Connection, investigation: Investigation):
    """All dataset_profile nodes, in load order (primary first)."""
    profiles = [
        n.payload.profile
        for n in node_store.list_by_investigation(conn, investigation.id)
        if isinstance(n.payload, ProfilePayload)
    ]
    if not profiles:
        raise ValueError("investigation has no dataset profile nodes")
    return profiles


# --------------------------------------------------------------------------- #
# The question loop
# --------------------------------------------------------------------------- #


def run_question(
    session: Session,
    question: str,
    parent_id: Optional[str] = None,
    progress: Optional[Progress] = None,
    fast: bool = False,
) -> RunResult:
    conn = session.conn
    inv_id = session.investigation.id
    p = progress or NullProgress()

    # An explicitly supplied parent means this is a branch off an earlier node
    # (e.g. `/branch`): scope context to that node's ancestry so the fork reasons
    # from its own lineage, not whatever the latest sibling turn was.
    branch_from = parent_id
    # Compact digest of the investigation so far (prior Q&A + recent results),
    # built BEFORE appending this question so it reflects earlier turns only.
    context = build_context(conn, inv_id, from_node_id=branch_from)
    # Thread this question under the previous conclusion so the graph reflects
    # what it follows from.
    if parent_id is None:
        parent_id = _thread_parent(conn, inv_id)

    q_node = node_store.append(
        conn,
        inv_id,
        QuestionPayload(question=question),
        title=_truncate(question, 80),
        parent_id=parent_id,
    )
    result = RunResult(question_node=q_node)

    # Full structured investigation (plan → steps/tools → narrate + follow-ups) is
    # the default. The fast path — a single agentic run_sql loop, fewer round-trips
    # but no tools/follow-ups — is opt-in via /quick. Both emit typed nodes.
    if _should_use_fast_path(session.client, fast):
        result.path = "fast"
        _run_fast(session, question, context, q_node, result, p)
        _update_rolling_summary(session)
        return result

    # --- full path ---
    # 1) plan
    p.start_planning()
    try:
        plan = planner.make_plan(session.client, question, session.profiles, context)
    except Exception as e:  # planning failed → record and stop
        result.error_nodes.append(_error(session, inv_id, q_node.id, "plan", str(e)))
        return result
    p.plan_ready(plan)

    plan_node = node_store.append(
        conn,
        inv_id,
        PlanPayload(plan=plan),
        title=f"Plan: {len(plan.steps)} step(s)",
        parent_id=q_node.id,
        model=session.client.name,
        prompt_version=PROMPT_VERSION,
    )
    result.plan_node = plan_node

    # 2) per-step: SQL query or analysis tool, chosen by the planner
    outcomes: List[StepOutcome] = []
    step_result_node: dict = {}  # step id -> its result node id (for depends_on edges)
    for i, step in enumerate(plan.steps):
        p.step_start(i, step)
        # pass results of earlier steps so a dependent step can compose on them
        outcome = _run_step(session, plan_node.id, step, result, prior_outcomes=outcomes)
        p.step_done(i, step, outcome is not None)
        if outcome is not None:
            outcomes.append(outcome)
            step_result_node[step.id] = outcome.node_id
    # planner-declared analytical dependencies → depends_on edges between results
    _capture_dependencies(session, plan.steps, step_result_node)

    # A zero-step plan (empty by design) still narrates — the answer comes straight
    # from the profile. Only bail if steps were planned but all failed.
    if plan.steps and not outcomes:
        return result

    # 3) narrate
    p.start_narrating()
    try:
        interpretation, conclusion, follow_ups = narrator.narrate(
            session.client, question, session.profiles, outcomes, context
        )
    except Exception as e:
        result.error_nodes.append(_error(session, inv_id, plan_node.id, "narrate", str(e)))
        return result

    result.interpretation_node = node_store.append(
        conn,
        inv_id,
        InterpretationPayload(interpretation=interpretation),
        title="Interpretation",
        parent_id=plan_node.id,
        model=session.client.name,
        prompt_version=PROMPT_VERSION,
    )
    result.conclusion_node = node_store.append(
        conn,
        inv_id,
        ConclusionPayload(conclusion=conclusion),
        title=_truncate(conclusion.summary, 80),
        parent_id=plan_node.id,
        model=session.client.name,
        prompt_version=PROMPT_VERSION,
    )
    # Deterministic structural edges: each evidence node → conclusion (created_by=engine,
    # trustworthy). These are the substrate for the graph checks and status projection.
    for ev_id in conclusion.evidence_node_ids:
        try:
            edge_store.add_edge(conn, inv_id, ev_id, result.conclusion_node.id,
                                EdgeType.supports, EdgeCreatedBy.engine)
        except Exception:
            pass  # a missing/duplicate edge must never break a run
    for fu in follow_ups:
        result.follow_up_nodes.append(
            node_store.append(
                conn,
                inv_id,
                FollowUpPayload(follow_up=fu),
                title=_truncate(fu.question, 80),
                parent_id=result.conclusion_node.id,
                model=session.client.name,
                prompt_version=PROMPT_VERSION,
            )
        )

    # auto-chart the headline result if it's chartable (deterministic, no LLM)
    _maybe_autochart(session, result)

    # deterministic lint of the conclusion (rule-based, no LLM) — always runs
    _lint_conclusion(session, result)

    # fold older turns into the rolling summary (best-effort; never fails a run)
    _update_rolling_summary(session)
    return result


def judge_investigation(session: Session, from_node_id: Optional[str] = None) -> Node:
    """On-demand LLM judge: review the whole investigation (or a branch's ancestry
    when ``from_node_id`` is given) and persist a critique (mode='review') node.

    Graph-aware in two ways: the deterministic graph checks are computed first and fed
    to the LLM as GRAPH WARNINGS, and the LLM's untested alternatives are materialized
    as first-class ``hypothesis`` nodes (with ``alternative_to`` edges) so later reviews
    can see which alternatives remain open."""
    conn, inv_id = session.conn, session.investigation.id
    graph_warnings = graph.graph_lint(conn, inv_id)
    payload = judge.review_investigation(
        session, from_node_id=from_node_id, graph_warnings=graph_warnings
    )
    parent_id = from_node_id or _thread_parent(conn, inv_id)
    review_node = _persist_review(session, payload, parent_id, title="Investigation review")
    # Materialize untested alternatives as hypothesis nodes (created_by=judge → the
    # alternative_to edge is a semantic assertion, so it lands as 'proposed').
    _materialize_alternatives(session, payload, review_node,
                              target=from_node_id or parent_id or review_node.id)
    return review_node


def judge_branches(session: Session, node_a: str, node_b: str) -> Node:
    """Compare two branches head-to-head (the pass that catches definition drift and
    shared-evidence circularity between competing explanations)."""
    conn, inv_id = session.conn, session.investigation.id
    scope = graph.ancestry_ids(conn, inv_id, node_a) | graph.ancestry_ids(conn, inv_id, node_b)
    directive = (
        f"These are two branches of one investigation — branch A ending at {node_a[:8]} "
        f"and branch B ending at {node_b[:8]}. Compare them head-to-head: (1) do they reach "
        "compatible conclusions, or do they conflict? (2) Do they operationalize the same "
        "metric or quantity DIFFERENTLY (definition drift) — if so, name it and say which is "
        "sounder. (3) Which explanation does the evidence better support? (4) Is any "
        "comparison between them undermined because they lean on the SAME evidence?"
    )
    payload = judge.review(session, scope_ids=scope, directive=directive,
                           graph_warnings=graph.graph_lint(conn, inv_id))
    return _persist_review(session, payload, _thread_parent(conn, inv_id),
                           title="Branch comparison")


def judge_unresolved(session: Session) -> Node:
    """Review focused on the still-open hypotheses: which remain live, and the single
    test that would resolve the most."""
    conn, inv_id = session.conn, session.investigation.id
    hyps = graph.unresolved_hypotheses(conn, inv_id)
    scope = None
    if hyps:
        scope = {h.id for h in hyps}
        for h in hyps:  # pull in each hypothesis's edge neighbours
            for e in edge_store.edges_into(conn, h.id) + edge_store.edges_from(conn, h.id):
                scope.add(e.source_id)
                scope.add(e.target_id)
    directive = (
        "Focus on the unresolved hypotheses. Which remain genuinely live given the evidence "
        "gathered so far, which look already-settled, and what SINGLE next test would resolve "
        "the most at once?"
    )
    payload = judge.review(session, scope_ids=scope, directive=directive,
                           graph_warnings=graph.graph_lint(conn, inv_id))
    return _persist_review(session, payload, _thread_parent(conn, inv_id),
                           title="Unresolved-hypotheses review")


def judge_descendants(session: Session, node_id: str) -> Node:
    """Review the subtree rooted at ``node_id`` (that node and everything beneath it)."""
    conn, inv_id = session.conn, session.investigation.id
    scope = graph.descendant_ids(conn, inv_id, node_id)
    payload = judge.review(session, scope_ids=scope, target_node_id=node_id,
                           graph_warnings=graph.graph_lint(conn, inv_id))
    return _persist_review(session, payload, node_id, title="Subtree review")


def define_metric(session: Session, name: str, sql: str, description: str = "") -> Node:
    """Add a named metric to the semantic layer. It becomes a first-class node and is
    fed into subsequent planning/SQL context so its definition is REUSED rather than
    re-derived (drift prevention)."""
    return node_store.append(
        session.conn, session.investigation.id,
        MetricPayload(name=name, sql=sql, description=description),
        title=f"metric: {name}", parent_id=None,
    )


def _capture_dependencies(session: Session, steps, step_result_node: dict) -> None:
    """Turn planner-declared step dependencies into depends_on edges between the
    steps' result nodes (created_by=narrator — the planner asserted them)."""
    conn, inv_id = session.conn, session.investigation.id
    for step in steps:
        dst = step_result_node.get(step.id)
        if not dst:
            continue
        for dep_step_id in getattr(step, "depends_on", []) or []:
            src = step_result_node.get(dep_step_id)
            if src:
                try:
                    edge_store.add_edge(conn, inv_id, dst, src,
                                        EdgeType.depends_on, EdgeCreatedBy.narrator)
                except Exception:
                    pass


def _persist_review(session: Session, payload, parent_id: Optional[str], title: str) -> Node:
    return node_store.append(
        session.conn, session.investigation.id, payload, title=title,
        parent_id=parent_id, model=session.client.name, prompt_version=PROMPT_VERSION,
    )


def _materialize_alternatives(session: Session, payload, review_node: Node, target: str) -> None:
    if payload.review is None:
        return
    conn, inv_id = session.conn, session.investigation.id
    # existing hypothesis statements — skip near-duplicates so repeated /judge runs
    # don't pile up the same alternative reworded.
    existing = [n.payload.statement
                for n in node_store.list_by_investigation(conn, inv_id)
                if isinstance(n.payload, HypothesisPayload)]
    for alt in payload.review.untested_alternatives:
        if any(graph.statements_similar(alt.hypothesis, e) for e in existing):
            continue
        existing.append(alt.hypothesis)
        hyp = node_store.append(
            conn, inv_id, HypothesisPayload(statement=alt.hypothesis, origin="judge"),
            title=_truncate(alt.hypothesis, 80), parent_id=review_node.id,
            model=session.client.name, prompt_version=PROMPT_VERSION,
        )
        try:
            edge_store.add_edge(conn, inv_id, hyp.id, target,
                                EdgeType.alternative_to, EdgeCreatedBy.judge,
                                status=EdgeStatus.proposed)
        except Exception:
            pass


def _lint_conclusion(session: Session, result: RunResult) -> None:
    """Run the deterministic linter over the finished turn and persist a critique
    (mode='lint') node under the conclusion. Best-effort; never fails a run."""
    if result.conclusion_node is None:
        return
    try:
        payload = judge.lint_result(result)
        if payload is None:
            return
        result.lint_node = node_store.append(
            session.conn,
            session.investigation.id,
            payload,
            title="Lint",
            parent_id=result.conclusion_node.id,
        )
    except Exception:
        pass  # a linter bug must never break an investigation


def _run_step(
    session: Session,
    plan_node_id: str,
    step: PlanStep,
    result: RunResult,
    prior_outcomes: List[StepOutcome] = (),
) -> Optional[StepOutcome]:
    """Execute one plan step (SQL query or analysis tool) and return its outcome."""
    if step.method == "tool" and step.tool:
        return _run_tool_step(session, plan_node_id, step, result)
    return _run_sql_step(session, plan_node_id, step, result, prior_outcomes)


def _run_sql_step(
    session: Session,
    plan_node_id: str,
    step: PlanStep,
    result: RunResult,
    prior_outcomes: List[StepOutcome] = (),
) -> Optional[StepOutcome]:
    conn = session.conn
    inv_id = session.investigation.id

    # Batch mode: use the SQL the planner emitted inline. Fall back to generating
    # it per-step only if the plan didn't carry a command. Guard either way.
    try:
        if step.sql:
            query = SqlQuery(step_id=step.id, sql=step.sql, notes="from plan (batch)")
        else:
            query = sqlgen.generate_sql(session.client, step, session.profiles, prior_outcomes)
        query.sql = guard_sql(query.sql)
    except Exception as e:
        result.error_nodes.append(_error(session, inv_id, plan_node_id, "sqlgen", str(e)))
        return None

    sql_node = node_store.append(
        conn,
        inv_id,
        SqlPayload(query=query),
        title=f"SQL: {step.intent}",
        parent_id=plan_node_id,
        model=session.client.name,
        prompt_version=PROMPT_VERSION,
    )

    # execute
    try:
        exec_result = executor.execute(session.duck, query.sql)
    except duckdb.Error as e:
        result.error_nodes.append(_error(session, inv_id, sql_node.id, "execute", str(e)))
        return None

    table = exec_result.to_result_table()
    table_node = node_store.append(
        conn,
        inv_id,
        TablePayload(table=table),
        title=f"Result: {table.row_count:,} rows",
        parent_id=sql_node.id,
    )

    # spill large results to parquet
    if table.row_count > INLINE_ROW_THRESHOLD:
        path = artifacts.table_path(session.paths, inv_id, table_node.id)
        if _spill_parquet(session.duck, query.sql, path):
            node_store.set_artifact_path(conn, table_node.id, str(path))

    result.table_nodes.append(table_node)
    return StepOutcome(step=step, node_id=table_node.id, table=table, sql=query.sql)


def _run_tool_step(
    session: Session, plan_node_id: str, step: PlanStep, result: RunResult
) -> Optional[StepOutcome]:
    inv_id = session.investigation.id

    # resolve the chosen tool
    try:
        tool = get_tool(step.tool)
    except KeyError as e:
        result.error_nodes.append(_error(session, inv_id, plan_node_id, "tool_select", str(e)))
        return None

    # Batch mode: use the tool args the planner emitted inline (JSON); fall back
    # to a per-step generation call only if the plan didn't carry them.
    try:
        if step.tool_args_json:
            call = ToolCall(tool=step.tool, inputs=json.loads(step.tool_args_json))
        else:
            call = session.client.generate_tool_call(step, session.profiles, tool)
    except Exception as e:
        result.error_nodes.append(_error(session, inv_id, plan_node_id, "tool_args", str(e)))
        return None

    # run_tool persists tool_call -> tool_result (or an error node) and returns it
    node = run_tool(session, call.tool, call.inputs, parent_id=plan_node_id)
    if isinstance(node.payload, ToolResultPayload):
        result.tool_nodes.append(node)
        return StepOutcome(step=step, node_id=node.id, tool_result=node.payload.result)
    result.error_nodes.append(node)
    return None


def rerun_sql_node(session: Session, sql_node: Node) -> Node:
    """Deterministically replay a stored SQL query (no LLM), appending a new
    table_result node linked to the original sql_query node."""
    if not isinstance(sql_node.payload, SqlPayload):
        raise ValueError(f"node {sql_node.id[:8]} is not a sql_query node")
    sql = guard_sql(sql_node.payload.query.sql)  # idempotent re-check
    exec_result = executor.execute(session.duck, sql)
    table = exec_result.to_result_table()
    node = node_store.append(
        session.conn,
        session.investigation.id,
        TablePayload(table=table),
        title=f"Rerun result: {table.row_count:,} rows",
        parent_id=sql_node.id,
    )
    if table.row_count > INLINE_ROW_THRESHOLD:
        path = artifacts.table_path(session.paths, session.investigation.id, node.id)
        if _spill_parquet(session.duck, sql, path):
            node_store.set_artifact_path(session.conn, node.id, str(path))
    return node


def chart_table_node(
    session: Session,
    table_node: Node,
    chart_type: str,
    x: str,
    y: Optional[str],
    title: Optional[str] = None,
) -> Node:
    """Render a chart of a stored table_result node → PNG artifact + chart node."""
    if not isinstance(table_node.payload, TablePayload):
        raise ValueError(f"node {table_node.id[:8]} is not a table_result node")
    table = table_node.payload.table
    title = title or (f"{y} by {x}" if y else x)
    chart_node = node_store.append(
        session.conn, session.investigation.id,
        ChartPayload(chart_type=chart_type, title=title, x=x, y=y, source_node_id=table_node.id),
        title=f"Chart: {title}", parent_id=table_node.id,
    )
    png = artifacts.chart_path(session.paths, session.investigation.id, chart_node.id)
    charts.render_png(table, chart_type, x, y, title, png)
    node_store.set_artifact_path(session.conn, chart_node.id, str(png))
    return node_store.get(session.conn, chart_node.id)


def _maybe_autochart(session: Session, result: RunResult) -> None:
    """Auto-visualize the first chartable result table (best-effort; never fails)."""
    for table_node in result.table_nodes:
        if not isinstance(table_node.payload, TablePayload):
            continue
        spec = charts.suggest_chart(table_node.payload.table)
        if not spec:
            continue
        try:
            node = chart_table_node(session, table_node, spec["chart_type"],
                                    spec["x"], spec["y"], spec["title"])
            result.chart_nodes.append(node)
        except Exception:
            pass
        return  # one auto-chart per question (the headline result)


def rerun_plan(session: Session, plan_node: Node) -> RunResult:
    """Deterministically re-run every command in a saved plan's task list (the SQL
    / tool args the planner emitted inline), appending fresh result nodes under the
    plan node. No LLM calls when the steps carry their commands."""
    if not isinstance(plan_node.payload, PlanPayload):
        raise ValueError(f"node {plan_node.id[:8]} is not a plan node")
    result = RunResult(question_node=plan_node)
    for step in plan_node.payload.plan.steps:
        _run_step(session, plan_node.id, step, result)
    return result


def _resolve_tool_table(session: Session, inputs: dict) -> DatasetProfile:
    """Pick the table a tool runs on. A tool is single-table, but that table need not
    be the primary one: it's whichever loaded table holds the columns the call references
    (all of them must be on one table). An explicit ``table`` input overrides. Falls back
    to the primary table when no column is referenced (e.g. a whole-table summary)."""
    profiles = session.profiles
    explicit = inputs.get("table")
    if explicit:
        for p in profiles:
            if p.table_name == explicit:
                return p
        raise ToolError(f"no loaded table named '{explicit}'. Loaded: "
                        + ", ".join(p.table_name for p in profiles))
    all_cols = {c.name for p in profiles for c in p.columns}
    referenced = {v for v in inputs.values() if isinstance(v, str) and v in all_cols}
    if not referenced:
        return session.primary_profile
    candidates = [p for p in profiles if referenced <= {c.name for c in p.columns}]
    if not candidates:
        raise ToolError(
            "the referenced columns (" + ", ".join(sorted(referenced)) + ") are not all "
            "on one table — join or derive them in a SQL step first, then run the tool on "
            "that result's columns.")
    # prefer the primary table when it qualifies (stable default), else the first match
    for p in candidates:
        if p.table_name == session.primary_profile.table_name:
            return p
    return candidates[0]


def run_tool(
    session: Session,
    tool_name: str,
    inputs: dict,
    parent_id: Optional[str] = None,
) -> Node:
    """Run a deterministic analysis tool, persisting tool_call → tool_result.

    On any input/precondition/execution error a single ``error`` node is
    appended (linked to the tool_call when one exists) and returned.
    """
    conn = session.conn
    inv_id = session.investigation.id

    try:
        tool = get_tool(tool_name)
    except KeyError as e:
        return _error(session, inv_id, parent_id, "tool_lookup", str(e))

    # record the requested call first so even a failure is inspectable
    call_node = node_store.append(
        conn,
        inv_id,
        ToolCallPayload(call=ToolCall(tool=tool_name, inputs=inputs)),
        title=f"Tool call: {tool_name}",
        parent_id=parent_id,
    )

    try:
        target = _resolve_tool_table(session, inputs)          # which table to run on
        parsed = tool.parse_inputs({k: v for k, v in inputs.items() if k != "table"})
        result = tool.run(session.duck, target.table_name, target, parsed)
    except ToolError as e:
        return _error(session, inv_id, call_node.id, "tool_input", str(e))
    except duckdb.Error as e:
        return _error(session, inv_id, call_node.id, "tool_execute", str(e))

    return node_store.append(
        conn,
        inv_id,
        ToolResultPayload(result=result),
        title=f"{tool_name}: {_truncate(result.summary, 60)}",
        parent_id=call_node.id,
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _should_use_fast_path(client, fast_requested: bool) -> bool:
    """Fast path only when explicitly requested (/quick) AND the backend supports
    it. The full structured investigation is the default — no keyword guessing."""
    return bool(fast_requested) and getattr(client, "supports_fast_path", False)


def _render_table_text(table, max_rows: int = 40) -> str:
    """Compact text rendering of a result table, fed back to the model."""
    if not table.columns:
        return "(no columns)"
    lines = [" | ".join(str(c) for c in table.columns)]
    for row in table.rows_preview[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    if table.row_count > min(len(table.rows_preview), max_rows):
        lines.append(f"... ({table.row_count} rows total)")
    return "\n".join(lines)[:4000]


def _run_fast(session: Session, question: str, context: str, q_node: Node,
              result: RunResult, p: Progress) -> None:
    """Agentic fast path: the model drives a read-only run_sql loop; each query is
    guarded + executed and persisted as sql_query -> table_result nodes, and the
    final answer becomes the conclusion. Fewer round-trips, same typed graph."""
    conn = session.conn
    inv_id = session.investigation.id
    p.phase("Investigating — writing & running SQL…")

    def run_sql(query: str) -> str:
        try:
            safe = guard_sql(query)
        except Exception as e:
            result.error_nodes.append(_error(session, inv_id, q_node.id, "sqlgen", str(e)))
            return f"ERROR (query rejected by read-only guard): {e}"
        sql_node = node_store.append(
            conn, inv_id,
            SqlPayload(query=SqlQuery(step_id="fast", sql=safe, notes="fast-path query")),
            title="SQL (fast path)", parent_id=q_node.id,
            model=session.client.name, prompt_version=PROMPT_VERSION,
        )
        try:
            exec_result = executor.execute(session.duck, safe)
        except duckdb.Error as e:
            result.error_nodes.append(_error(session, inv_id, sql_node.id, "execute", str(e)))
            return f"ERROR executing: {e}"
        table = exec_result.to_result_table()
        table_node = node_store.append(
            conn, inv_id, TablePayload(table=table),
            title=f"Result: {table.row_count:,} rows", parent_id=sql_node.id,
        )
        if table.row_count > INLINE_ROW_THRESHOLD:
            path = artifacts.table_path(session.paths, inv_id, table_node.id)
            if _spill_parquet(session.duck, safe, path):
                node_store.set_artifact_path(conn, table_node.id, str(path))
        result.table_nodes.append(table_node)
        return _render_table_text(table)

    try:
        answer = session.client.investigate(question, session.profiles, context, run_sql)
    except Exception as e:
        result.error_nodes.append(_error(session, inv_id, q_node.id, "investigate", str(e)))
        return

    p.phase("Wrapping up…")
    result.conclusion_node = node_store.append(
        conn, inv_id,
        ConclusionPayload(conclusion=Conclusion(summary=answer or "(no answer produced)",
                                                confidence="medium")),
        title=_truncate(answer or "answer", 80), parent_id=q_node.id,
        model=session.client.name, prompt_version=PROMPT_VERSION,
    )


def _update_rolling_summary(session: Session) -> None:
    """Fold turns older than the verbatim window into a rolling summary node, so
    long investigations keep early findings without re-sending every turn. Best
    effort: a summarization failure must never break the question."""
    conn = session.conn
    inv_id = session.investigation.id
    try:
        nodes = node_store.list_by_investigation(conn, inv_id)
        # ordered (question, conclusion) turns
        conclusions = [n for n in nodes if isinstance(n.payload, ConclusionPayload)]
        turns = []  # (question_text, conclusion_seq, conclusion_summary)
        for q in (n for n in nodes if isinstance(n.payload, QuestionPayload)):
            concl = next((c for c in conclusions if c.seq > q.seq), None)
            if concl:
                turns.append((q.payload.question, concl.seq, concl.payload.conclusion.summary))

        if len(turns) <= VERBATIM_TURNS:
            return  # nothing has aged out of the verbatim window yet

        to_cover = turns[:-VERBATIM_TURNS]  # older turns that belong in the summary
        summaries = [n for n in nodes if isinstance(n.payload, SummaryPayload)]
        through = summaries[-1].payload.through_seq if summaries else 0
        prior_text = summaries[-1].payload.text if summaries else ""

        new = [(q, s) for (q, cseq, s) in to_cover if cseq > through]
        if not new:
            return

        text = session.client.summarize(prior_text, new)
        last_seq = to_cover[-1][1]
        node_store.append(
            conn,
            inv_id,
            SummaryPayload(text=text, through_seq=last_seq),
            title=f"Investigation summary (through {len(to_cover)} turns)",
            model=session.client.name,
            prompt_version=PROMPT_VERSION,
        )
    except Exception:
        # context aid only — swallow failures so the answer still stands
        pass


def _thread_parent(conn: sqlite3.Connection, investigation_id: str) -> Optional[str]:
    """Parent for a new question: the most recent conclusion, so consecutive
    questions form a thread. Falls back to None (a fresh root) if there is none."""
    nodes = node_store.list_by_investigation(conn, investigation_id)
    for node in reversed(nodes):
        if node.kind == NodeKind.conclusion:
            return node.id
    return None


def _error(session: Session, inv_id: str, parent_id: str, stage: str, message: str) -> Node:
    return node_store.append(
        session.conn,
        inv_id,
        ErrorPayload(message=message, stage=stage),
        title=f"Error in {stage}",
        parent_id=parent_id,
        status=NodeStatus.error,
        error=message,
    )


def _spill_parquet(duck: duckdb.DuckDBPyConnection, sql: str, path: Path) -> bool:
    """Persist a result to parquet using a separate, app-controlled writable
    connection (the user-data connection stays read-only). Best-effort: returns
    False if arrow support is unavailable."""
    try:
        table = duck.execute(sql).fetch_arrow_table()
    except Exception:
        return False
    writer = duckdb.connect()
    try:
        writer.register("_exhibit_spill", table)
        writer.execute("COPY _exhibit_spill TO ? (FORMAT PARQUET)", [str(path)])
    except Exception:
        return False
    finally:
        writer.close()
    return True


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
