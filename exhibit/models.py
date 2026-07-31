"""Structured contracts for investigations, nodes, and LLM outputs.

Two families of models live here:

1. **Persistence models** — ``Investigation`` and ``Node``. A ``Node`` is the
   atomic unit of an investigation; its ``content`` is a kind-specific payload
   validated through a discriminated union (``NodePayload``). Nodes are
   append-only and carry a ``parent_id`` so an investigation forms a tree/DAG
   without a separate edge concept.

2. **Engine / LLM contracts** — ``Plan``, ``PlanStep``, ``SqlQuery``,
   ``ResultTable``, ``Interpretation``, ``Conclusion``, ``FollowUp``. These are
   the structured shapes the (mock or real) LLM must return, and what the
   executor produces.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class NodeKind(str, Enum):
    dataset_profile = "dataset_profile"
    user_question = "user_question"
    plan = "plan"
    sql_query = "sql_query"
    table_result = "table_result"
    tool_call = "tool_call"
    tool_result = "tool_result"
    chart = "chart"
    interpretation = "interpretation"
    conclusion = "conclusion"
    follow_up = "follow_up"
    summary = "summary"
    hypothesis = "hypothesis"
    metric = "metric"
    critique = "critique"
    error = "error"


class NodeStatus(str, Enum):
    ok = "ok"
    error = "error"
    running = "running"


# --------------------------------------------------------------------------- #
# Engine / LLM contracts
# --------------------------------------------------------------------------- #

ExpectedOutput = Literal["table", "chart"]


class ColumnProfile(BaseModel):
    name: str
    dtype: str
    null_fraction: float
    distinct_count: Optional[int] = None
    min: Optional[str] = None
    max: Optional[str] = None
    sample_values: List[str] = Field(default_factory=list)


class DatasetProfile(BaseModel):
    table_name: str
    row_count: int
    columns: List[ColumnProfile]
    # Snapshot provenance (None for local files). Makes staleness visible rather than
    # hidden: where the data came from, when it was copied, and how big the source was.
    source: Optional[str] = None            # redacted origin, e.g. "postgresql://host:5432/db"
    snapshot_at: Optional[str] = None       # ISO timestamp the snapshot was taken
    source_row_count: Optional[int] = None  # rows in the source (> row_count if capped)

    @property
    def truncated(self) -> bool:
        return self.source_row_count is not None and self.source_row_count > self.row_count

    def schema_map(self) -> Dict[str, str]:
        return {c.name: c.dtype for c in self.columns}


StepMethod = Literal["sql", "tool"]


class PlanStep(BaseModel):
    id: str
    intent: str  # short machine-ish label, e.g. "monthly_revenue_trend"
    description: str  # natural-language analytical goal (NOT SQL)
    expected_output: ExpectedOutput = "table"
    # How this step is executed: a custom SQL query, or a named analysis tool.
    method: StepMethod = "sql"
    tool: Optional[str] = None  # registry tool name when method == "tool"
    # Batch mode: the planner emits the executable command inline, so the whole
    # ordered task list is saved in the plan node and re-runnable. `sql` for
    # method="sql"; `tool_args_json` (a JSON object string) for method="tool".
    # If absent, the engine falls back to generating the command per step.
    sql: Optional[str] = None
    tool_args_json: Optional[str] = None
    # Analytical dependency: ids of earlier steps this step BUILDS ON (reuses their
    # result as a CTE/subquery/scope). The planner declares this — it knows, because it
    # composed the steps — and the engine turns it into depends_on edges. Distinct from
    # mere ordering: two steps can be sequential without one depending on the other.
    depends_on: List[str] = Field(default_factory=list)


class Plan(BaseModel):
    question: str
    rationale: str
    steps: List[PlanStep]


class SqlQuery(BaseModel):
    step_id: str
    sql: str
    reads_columns: List[str] = Field(default_factory=list)
    notes: str = ""


class ResultTable(BaseModel):
    columns: List[str]
    rows_preview: List[List[Any]] = Field(default_factory=list)  # <= preview cap
    row_count: int
    parquet_path: Optional[str] = None  # set when spilled to disk


class ToolCall(BaseModel):
    """A request to run a deterministic analysis tool with validated inputs."""

    tool: str
    inputs: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Compact, LLM-facing output of a tool run.

    ``summary`` + ``metrics`` are small enough to feed straight into context;
    any supporting rows go in ``table`` (kept small) or are spilled as an
    artifact. ``caveats`` surface statistical assumptions that were or weren't
    met, so a downstream model doesn't over-claim.
    """

    tool: str
    summary: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    table: Optional["ResultTable"] = None
    caveats: List[str] = Field(default_factory=list)


class StepOutcome(BaseModel):
    """Result of executing one plan step, for narration. Carries either a SQL
    result table or a tool result (never both), plus the node id it produced so
    the narrator can cite it as evidence."""

    step: PlanStep
    node_id: str
    table: Optional[ResultTable] = None
    tool_result: Optional[ToolResult] = None
    sql: Optional[str] = None  # the executed SQL (sql steps) — lets later steps reuse it


class Interpretation(BaseModel):
    findings: List[str]
    evidence_node_ids: List[str] = Field(default_factory=list)


class Conclusion(BaseModel):
    summary: str
    confidence: Literal["low", "medium", "high"] = "medium"
    evidence_node_ids: List[str] = Field(default_factory=list)


class FollowUp(BaseModel):
    question: str
    why: str


class NarrationResponse(BaseModel):
    """What the LLM returns for narration. Evidence node ids are filled in by the
    engine (the model never sees or invents node ids), keeping citations grounded."""

    findings: List[str]
    summary: str
    confidence: Literal["low", "medium", "high"] = "medium"
    follow_ups: List[FollowUp] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Node payloads (discriminated union on ``kind``)
# --------------------------------------------------------------------------- #


class _Payload(BaseModel):
    kind: NodeKind


class ProfilePayload(_Payload):
    kind: Literal[NodeKind.dataset_profile] = NodeKind.dataset_profile
    profile: DatasetProfile


class QuestionPayload(_Payload):
    kind: Literal[NodeKind.user_question] = NodeKind.user_question
    question: str


class PlanPayload(_Payload):
    kind: Literal[NodeKind.plan] = NodeKind.plan
    plan: Plan


class SqlPayload(_Payload):
    kind: Literal[NodeKind.sql_query] = NodeKind.sql_query
    query: SqlQuery


class TablePayload(_Payload):
    kind: Literal[NodeKind.table_result] = NodeKind.table_result
    table: ResultTable


class ToolCallPayload(_Payload):
    kind: Literal[NodeKind.tool_call] = NodeKind.tool_call
    call: ToolCall


class ToolResultPayload(_Payload):
    kind: Literal[NodeKind.tool_result] = NodeKind.tool_result
    result: ToolResult


class ChartPayload(_Payload):
    kind: Literal[NodeKind.chart] = NodeKind.chart
    chart_type: Literal["line", "bar", "scatter", "histogram"]
    title: str
    x: str
    y: Optional[str] = None  # not needed for histogram
    source_node_id: Optional[str] = None  # the table_result this visualizes


class InterpretationPayload(_Payload):
    kind: Literal[NodeKind.interpretation] = NodeKind.interpretation
    interpretation: Interpretation


class ConclusionPayload(_Payload):
    kind: Literal[NodeKind.conclusion] = NodeKind.conclusion
    conclusion: Conclusion


class FollowUpPayload(_Payload):
    kind: Literal[NodeKind.follow_up] = NodeKind.follow_up
    follow_up: FollowUp


class SummaryPayload(_Payload):
    kind: Literal[NodeKind.summary] = NodeKind.summary
    text: str
    through_seq: int  # summarizes all turns whose conclusion seq <= this


class HypothesisPayload(_Payload):
    kind: Literal[NodeKind.hypothesis] = NodeKind.hypothesis
    statement: str
    origin: Literal["judge", "narrator", "user"] = "judge"
    # NB: no stored ``status`` — a hypothesis's status is a projection over its edges
    # (see engine.graph.hypothesis_status), never a mutable field.


class MetricPayload(_Payload):
    kind: Literal[NodeKind.metric] = NodeKind.metric
    name: str                       # e.g. "squad_value"
    sql: str                        # the canonical SQL expression / definition
    description: str = ""           # what it means / how to read it


class DeterministicCheck(BaseModel):
    """One rule-based lint check over a conclusion + its evidence. No LLM."""

    name: str
    status: Literal["ok", "warn"]
    detail: str
    node_ids: List[str] = Field(default_factory=list)  # real ids the check refers to


class WeakClaim(BaseModel):
    claim: str
    why: str


class Claim(BaseModel):
    """One atomic assertion decomposed out of a conclusion paragraph, judged on its own.
    A causal claim in a paragraph can be speculative while a descriptive one beside it is
    solid — this lets the judge say so per-claim instead of rating the whole paragraph."""
    text: str
    claim_type: Literal["descriptive", "comparative", "causal", "forecast"]
    verdict: Literal["supported", "weak", "unsupported"]
    why: str


class Alternative(BaseModel):
    hypothesis: str
    how_to_test: str


class JudgeReview(BaseModel):
    """The on-demand LLM judge's structured critique of an investigation.

    Deliberately holds NO node ids — the model never invents citations; grounding
    to specific nodes is the deterministic linter's job."""

    overall: str
    claims: List[Claim] = Field(default_factory=list)   # the conclusion, decomposed
    assumptions: List[str] = Field(default_factory=list)
    weak_conclusions: List[WeakClaim] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    untested_alternatives: List[Alternative] = Field(default_factory=list)
    simpler_explanation: Optional[str] = None
    confidence_assessment: str = ""
    next_query: FollowUp


class CritiquePayload(_Payload):
    kind: Literal[NodeKind.critique] = NodeKind.critique
    mode: Literal["lint", "review"]
    target_node_id: Optional[str] = None       # the conclusion/subtree this critiques
    checks: List[DeterministicCheck] = Field(default_factory=list)  # mode == "lint"
    review: Optional[JudgeReview] = None        # mode == "review"


class ErrorPayload(_Payload):
    kind: Literal[NodeKind.error] = NodeKind.error
    message: str
    stage: str = ""


NodePayload = Annotated[
    Union[
        ProfilePayload,
        QuestionPayload,
        PlanPayload,
        SqlPayload,
        TablePayload,
        ToolCallPayload,
        ToolResultPayload,
        ChartPayload,
        InterpretationPayload,
        ConclusionPayload,
        FollowUpPayload,
        SummaryPayload,
        HypothesisPayload,
        MetricPayload,
        CritiquePayload,
        ErrorPayload,
    ],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Typed analytical edges (the DAG alongside the conversational tree)
# --------------------------------------------------------------------------- #


class EdgeType(str, Enum):
    supports = "supports"          # evidence → claim/conclusion/hypothesis
    contradicts = "contradicts"    # evidence → claim/conclusion/hypothesis
    refines = "refines"
    supersedes = "supersedes"
    tests = "tests"                # step/result → hypothesis
    depends_on = "depends_on"      # analytical input (not mere chronology)
    alternative_to = "alternative_to"
    derived_from = "derived_from"


class EdgeCreatedBy(str, Enum):
    engine = "engine"      # deterministic, trustworthy
    narrator = "narrator"  # asserted by the analyst LLM
    judge = "judge"        # asserted by the (independent) judge
    user = "user"


class EdgeStatus(str, Enum):
    active = "active"
    proposed = "proposed"      # asserted semantic edge, awaiting acceptance
    rejected = "rejected"
    superseded = "superseded"


# Which edge relationships are deterministic/trustworthy (engine-captured) vs
# semantic assertions that must land as ``proposed`` until independently accepted.
STRUCTURAL_EDGES = {EdgeType.supports, EdgeType.tests, EdgeType.depends_on,
                    EdgeType.derived_from}
SEMANTIC_EDGES = {EdgeType.contradicts, EdgeType.alternative_to, EdgeType.refines,
                  EdgeType.supersedes}


class Edge(BaseModel):
    id: str
    investigation_id: str
    source_id: str
    target_id: str
    relationship: EdgeType
    created_by: EdgeCreatedBy
    status: EdgeStatus = EdgeStatus.active
    created_at: str


# --------------------------------------------------------------------------- #
# Persistence models
# --------------------------------------------------------------------------- #


class Node(BaseModel):
    id: str
    investigation_id: str
    seq: int
    parent_id: Optional[str]
    kind: NodeKind
    created_at: str
    title: str
    payload: NodePayload
    artifact_path: Optional[str] = None
    status: NodeStatus = NodeStatus.ok
    error: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None


class Investigation(BaseModel):
    id: str
    name: str
    created_at: str
    data_path: str
    data_format: Literal["csv", "parquet", "postgres"]
    table_name: str
    schema_map: Dict[str, str] = Field(default_factory=dict)
    profile_node_id: Optional[str] = None
