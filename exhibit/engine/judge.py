"""Investigation judge — two levels of self-review.

1. A **deterministic linter** (`lint_result`) runs after every full-investigation
   conclusion. It is pure, rule-based, and cheap (no LLM): it inspects the turn's
   conclusion and the evidence nodes it rests on and flags structural weaknesses —
   ungrounded claims, steps that errored, causal language with no statistical test,
   high confidence on thin evidence, possibly-truncated results. It cites real node
   ids because it computes them.

2. An **LLM investigation judge** (`review_investigation`) runs on demand (`/judge`)
   at checkpoints. It reads the whole investigation (or a branch's ancestry) and
   returns a structured, skeptical critique: assumptions, weakly-supported
   conclusions, missing evidence, untested alternatives, a simpler-explanation
   check, a confidence assessment, and the single most valuable next query. It never
   invents node ids — grounding is the linter's job.

This is the adversary from the evals turned inward and made a first-class node.
"""

from __future__ import annotations

import re
import sqlite3
from typing import List, Optional, TYPE_CHECKING

from ..config import DEFAULT_ROW_LIMIT
from ..models import (
    ConclusionPayload,
    CritiquePayload,
    DeterministicCheck,
    Node,
    QuestionPayload,
    TablePayload,
    ToolResultPayload,
)
from ..store import nodes as node_store

if TYPE_CHECKING:  # avoid import cycles
    from .orchestrator import RunResult

# Tools that constitute an actual statistical test / comparison (vs. plain describe).
_TEST_TOOLS = {
    "t_test", "mann_whitney", "chi_square", "correlation", "correlation_matrix",
    "linear_fit", "logistic_fit", "trend",
}
_CAUSAL = re.compile(
    r"\b(caus(e|ed|es|ing)|because|due to|drove|driven by|led to|leads? to|"
    r"responsible for|explains?|thanks to|resulted? in|the reason)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 1) Deterministic linter (no LLM)
# --------------------------------------------------------------------------- #

def lint_result(result: "RunResult") -> Optional[CritiquePayload]:
    """Rule-based lint of a completed full-investigation turn. Returns a
    ``CritiquePayload`` (mode='lint') or None if there's no conclusion to judge."""
    concl_node = result.conclusion_node
    if concl_node is None or not isinstance(concl_node.payload, ConclusionPayload):
        return None
    conclusion = concl_node.payload.conclusion
    summary = conclusion.summary

    evidence = result.table_nodes + result.tool_nodes
    n_evidence = len(evidence)
    tool_names = {
        n.payload.result.tool
        for n in result.tool_nodes
        if isinstance(n.payload, ToolResultPayload)
    }
    planned_steps = 0
    if result.plan_node is not None and hasattr(result.plan_node.payload, "plan"):
        planned_steps = len(result.plan_node.payload.plan.steps)

    checks: List[DeterministicCheck] = []

    # a) groundedness
    if planned_steps > 0 and n_evidence == 0:
        checks.append(DeterministicCheck(
            name="groundedness", status="warn",
            detail="The conclusion rests on no successful result — every planned step "
                   "produced no table or tool output.",
            node_ids=[concl_node.id]))
    else:
        checks.append(DeterministicCheck(
            name="groundedness", status="ok",
            detail=f"Conclusion is backed by {n_evidence} evidence node(s)."))

    # b) step errors
    if result.error_nodes:
        checks.append(DeterministicCheck(
            name="step_errors", status="warn",
            detail=f"{len(result.error_nodes)} step(s) errored; the conclusion may rest "
                   "on partial evidence.",
            node_ids=[n.id for n in result.error_nodes]))

    # c) causal language without a statistical test
    if _CAUSAL.search(summary) and not (tool_names & _TEST_TOOLS):
        checks.append(DeterministicCheck(
            name="causal_claim", status="warn",
            detail="The conclusion uses causal language but no statistical test "
                   "(t_test / correlation / trend …) or explicit control was run — "
                   "the claim is associational at best.",
            node_ids=[concl_node.id]))

    # d) confidence calibration
    if conclusion.confidence == "high" and (n_evidence <= 1 or result.error_nodes):
        checks.append(DeterministicCheck(
            name="confidence_calibration", status="warn",
            detail="Stated confidence is 'high' but the evidence is thin or partial "
                   f"({n_evidence} result(s), {len(result.error_nodes)} error(s)).",
            node_ids=[concl_node.id]))

    # e) possible truncation at the row cap
    truncated = [
        n.id for n in result.table_nodes
        if isinstance(n.payload, TablePayload)
        and n.payload.table.row_count >= DEFAULT_ROW_LIMIT
    ]
    if truncated:
        checks.append(DeterministicCheck(
            name="truncation", status="warn",
            detail=f"A result hit the {DEFAULT_ROW_LIMIT}-row cap and may be truncated; "
                   "aggregate or narrow the query before drawing conclusions from it.",
            node_ids=truncated))

    return CritiquePayload(mode="lint", target_node_id=concl_node.id, checks=checks)


def lint_warnings(payload: CritiquePayload) -> List[DeterministicCheck]:
    return [c for c in payload.checks if c.status == "warn"]


# --------------------------------------------------------------------------- #
# 2) LLM investigation judge (on demand)
# --------------------------------------------------------------------------- #

def build_review_context(
    conn: sqlite3.Connection, investigation_id: str, scope_ids=None
) -> str:
    """Chronological transcript (secondary view): every question with its conclusion,
    confidence, evidence and errors. ``scope_ids`` restricts to a subgraph."""
    nodes = node_store.list_by_investigation(conn, investigation_id)
    if scope_ids is not None:
        scope = set(scope_ids)
        nodes = [n for n in nodes if n.id in scope]

    lines: List[str] = []
    for n in nodes:
        p = n.payload
        if isinstance(p, QuestionPayload):
            lines.append(f"\nQ: {p.question}")
        elif isinstance(p, ConclusionPayload):
            c = p.conclusion
            lines.append(f"  Conclusion [{c.confidence}]: {c.summary}")
        elif isinstance(p, TablePayload):
            lines.append(f"  · table result: [{', '.join(p.table.columns)}] "
                         f"({p.table.row_count} rows)")
        elif isinstance(p, ToolResultPayload):
            lines.append(f"  · tool {p.result.tool}: {p.result.summary}")
        elif n.kind.value == "error":
            lines.append(f"  · ERROR: {getattr(p, 'message', '')}")
    return "\n".join(lines).strip()


def assemble_judge_input(
    conn: sqlite3.Connection,
    investigation_id: str,
    scope_ids=None,
    directive: Optional[str] = None,
    graph_warnings=None,
) -> str:
    """The graph-aware judge input: a structured GRAPH STRUCTURE section (relationships
    the judge can traverse) first, then deterministic GRAPH WARNINGS, an optional
    DIRECTIVE (e.g. compare two branches), and the chronological transcript last as a
    secondary, narrative view."""
    from . import graph  # local import to avoid a cycle

    digest = graph.build_graph_digest(conn, investigation_id, scope_ids)
    transcript = build_review_context(conn, investigation_id, scope_ids)

    parts = []
    if digest:
        parts.append("GRAPH STRUCTURE (relationships — reason over these):\n" + digest)
    if graph_warnings:
        wl = "\n".join(f"- [{c.name}] {c.detail}" for c in graph_warnings)
        parts.append("GRAPH WARNINGS (deterministic, from the edge graph):\n" + wl)
    if directive:
        parts.append("FOCUS FOR THIS REVIEW:\n" + directive)
    parts.append("CHRONOLOGICAL TRANSCRIPT (secondary — narrative order):\n" + transcript)
    return "\n\n".join(parts)


def review(
    session,
    scope_ids=None,
    target_node_id: Optional[str] = None,
    directive: Optional[str] = None,
    graph_warnings=None,
) -> CritiquePayload:
    """Run the on-demand LLM judge over a (possibly scoped) view of the graph."""
    context = assemble_judge_input(
        session.conn, session.investigation.id, scope_ids, directive, graph_warnings
    )
    return CritiquePayload(mode="review", target_node_id=target_node_id,
                           review=session.client.critique(context))


def review_investigation(
    session, from_node_id: Optional[str] = None, graph_warnings=None
) -> CritiquePayload:
    """Whole-investigation (or a branch's ancestry) review — the default `/judge`."""
    from . import graph
    scope_ids = None
    if from_node_id is not None:
        scope_ids = graph.ancestry_ids(session.conn, session.investigation.id, from_node_id)
    return review(session, scope_ids=scope_ids, target_node_id=from_node_id,
                  graph_warnings=graph_warnings)
