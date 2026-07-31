"""Graph-level reasoning over the typed edge DAG (deterministic; no LLM).

Two things live here:

- ``hypothesis_status`` — a hypothesis's status is a *projection* over its edges, not
  a stored field. Only ``active`` (accepted) edges count; ``proposed`` semantic edges
  do not flip a status until independently accepted.
- ``graph_lint`` — deterministic checks that require *relationships*, so the per-turn
  linter (which sees one turn) can't make them: shared evidence across branches,
  orphaned conclusions, and untested hypotheses.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Set

from typing import Iterable, Optional

from ..models import (
    ConclusionPayload,
    DeterministicCheck,
    Edge,
    EdgeStatus,
    EdgeType,
    HypothesisPayload,
    MetricPayload,
    Node,
    NodeKind,
    QuestionPayload,
    TablePayload,
    ToolResultPayload,
)
from ..store import edges as edge_store
from ..store import nodes as node_store


# --------------------------------------------------------------------------- #
# Near-duplicate hypothesis detection (so the judge doesn't pile up the same
# alternative worded slightly differently across repeated /judge runs).
# --------------------------------------------------------------------------- #

_STOP = {"the", "a", "an", "of", "to", "in", "by", "and", "or", "is", "are", "was",
         "were", "with", "from", "for", "on", "at", "as", "that", "this", "it", "its",
         "via", "into", "not", "but", "than", "then", "so", "each", "those", "these",
         "creates", "create", "driven", "drive"}


def _tokens(s: str) -> Set[str]:
    out: Set[str] = set()
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if len(w) <= 2 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s"):  # crude stem: matches→matche, differs→differ
            w = w[:-1]
        out.add(w)
    return out


def statements_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """True if two hypothesis statements are near-duplicates — overlap coefficient
    (shared tokens / smaller set) over the threshold. Catches the same idea reworded."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


# --------------------------------------------------------------------------- #
# Hypothesis status as a projection over edges
# --------------------------------------------------------------------------- #

def hypothesis_status(conn: sqlite3.Connection, hypothesis_node_id: str) -> str:
    """Derive a hypothesis's status from its (accepted) edges. Never stored."""
    incoming = [e for e in edge_store.edges_into(conn, hypothesis_node_id)
                if e.status == EdgeStatus.active]
    tested = any(e.relationship == EdgeType.tests for e in incoming)
    supports = sum(e.relationship == EdgeType.supports for e in incoming)
    contradicts = sum(e.relationship == EdgeType.contradicts for e in incoming)

    if supports and contradicts:
        return "weakened"
    if contradicts and not supports:
        return "rejected"
    if supports and not contradicts:
        return "supported"
    # no accepted supporting/contradicting evidence
    return "unresolved" if tested else "proposed"


# --------------------------------------------------------------------------- #
# Deterministic graph checks
# --------------------------------------------------------------------------- #

def _by_id(nodes: List[Node]) -> Dict[str, Node]:
    return {n.id: n for n in nodes}


def _ancestors(by_id: Dict[str, Node], node_id: str) -> Set[str]:
    """Conversational ancestry via parent_id (up to the root)."""
    out: Set[str] = set()
    cur = by_id.get(node_id)
    while cur is not None and cur.parent_id is not None:
        out.add(cur.parent_id)
        cur = by_id.get(cur.parent_id)
    return out


def _same_line(by_id: Dict[str, Node], a: str, b: str) -> bool:
    """True if a and b are on the same conversational line (one descends from the
    other), i.e. NOT divergent branches."""
    return a == b or a in _ancestors(by_id, b) or b in _ancestors(by_id, a)


# --------------------------------------------------------------------------- #
# Scope selectors (which nodes a judge pass should see)
# --------------------------------------------------------------------------- #

def ancestry_ids(conn: sqlite3.Connection, investigation_id: str, node_id: str) -> Set[str]:
    """A node and all its conversational ancestors (root → node)."""
    by_id = _by_id(node_store.list_by_investigation(conn, investigation_id))
    out = {node_id} if node_id in by_id else set()
    out |= _ancestors(by_id, node_id)
    return out


def descendant_ids(conn: sqlite3.Connection, investigation_id: str, node_id: str) -> Set[str]:
    """A node and everything that (transitively) hangs beneath it via parent_id."""
    nodes = node_store.list_by_investigation(conn, investigation_id)
    children: Dict[str, List[str]] = {}
    for n in nodes:
        if n.parent_id:
            children.setdefault(n.parent_id, []).append(n.id)
    out, stack = set(), [node_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, []))
    return out


def unresolved_hypotheses(conn: sqlite3.Connection, investigation_id: str) -> List[Node]:
    """Hypothesis nodes whose derived status is still open (proposed / unresolved)."""
    nodes = node_store.list_by_investigation(conn, investigation_id)
    return [n for n in nodes
            if isinstance(n.payload, HypothesisPayload)
            and hypothesis_status(conn, n.id) in ("proposed", "unresolved")]


# --------------------------------------------------------------------------- #
# Graph digest — a structured (not flattened) view for the judge
# --------------------------------------------------------------------------- #

def _brief(n: Node) -> str:
    p = n.payload
    if isinstance(p, ConclusionPayload):
        return f"conclusion[{n.id[:8]}] ({p.conclusion.confidence}): {p.conclusion.summary[:160]}"
    if isinstance(p, HypothesisPayload):
        return f"hypothesis[{n.id[:8]}]: {p.statement[:160]}"
    if isinstance(p, TablePayload):
        return f"table[{n.id[:8]}] [{', '.join(p.table.columns)}] ({p.table.row_count} rows)"
    if isinstance(p, ToolResultPayload):
        return f"tool[{n.id[:8]}] {p.result.tool}: {p.result.summary[:120]}"
    if isinstance(p, QuestionPayload):
        return f"question[{n.id[:8]}]: {p.question[:120]}"
    return f"{n.kind.value}[{n.id[:8]}]"


def build_graph_digest(
    conn: sqlite3.Connection, investigation_id: str, scope_ids: Optional[Iterable[str]] = None
) -> str:
    """A *structured* view of the investigation for the judge — hypotheses with their
    derived status, each conclusion with the evidence that supports it, and the semantic
    relationships (alternative_to / contradicts / supersedes) between nodes. This is what
    lets the judge reason over relationships that a flat transcript can't express."""
    nodes = node_store.list_by_investigation(conn, investigation_id)
    if scope_ids is not None:
        scope = set(scope_ids)
        nodes = [n for n in nodes if n.id in scope]
    by_id = _by_id(nodes)
    ids = set(by_id)
    edges = [e for e in edge_store.list_by_investigation(conn, investigation_id)
             if e.source_id in ids and e.target_id in ids]

    lines: List[str] = []

    hyps = [n for n in nodes if isinstance(n.payload, HypothesisPayload)]
    if hyps:
        lines.append("HYPOTHESES (status derived from edges):")
        for h in hyps:
            lines.append(f"  - [{hypothesis_status(conn, h.id)}] {h.payload.statement} "
                         f"({h.payload.origin})")

    concls = [n for n in nodes if isinstance(n.payload, ConclusionPayload)]
    if concls:
        lines.append("\nCLAIMS & THEIR EVIDENCE:")
        for c in concls:
            support = [by_id[e.source_id] for e in edges
                       if e.relationship == EdgeType.supports and e.target_id == c.id
                       and e.status == EdgeStatus.active and e.source_id in by_id]
            ev = "; ".join(_brief(s) for s in support) or "(no recorded evidence — orphan)"
            lines.append(f"  - {_brief(c)}")
            lines.append(f"      supported by: {ev}")

    deps = [e for e in edges if e.relationship == EdgeType.depends_on]
    if deps:
        lines.append("\nDEPENDENCIES (result → the earlier result it builds on):")
        for e in deps:
            s, t = by_id.get(e.source_id), by_id.get(e.target_id)
            if s and t:
                lines.append(f"  - {_brief(s)}  depends_on  {_brief(t)}")

    sem = [e for e in edges if e.relationship in (
        EdgeType.alternative_to, EdgeType.contradicts, EdgeType.supersedes, EdgeType.refines)]
    if sem:
        lines.append("\nRELATIONSHIPS:")
        for e in sem:
            s, t = by_id.get(e.source_id), by_id.get(e.target_id)
            if s and t:
                lines.append(f"  - {_brief(s)}  --{e.relationship.value}[{e.status.value}]-->  {_brief(t)}")

    return "\n".join(lines).strip()


def graph_lint(conn: sqlite3.Connection, investigation_id: str) -> List[DeterministicCheck]:
    nodes = node_store.list_by_investigation(conn, investigation_id)
    by_id = _by_id(nodes)
    all_edges = edge_store.list_by_investigation(conn, investigation_id)
    active = [e for e in all_edges if e.status == EdgeStatus.active]

    conclusions = [n for n in nodes if isinstance(n.payload, ConclusionPayload)]
    hypotheses = [n for n in nodes if isinstance(n.payload, HypothesisPayload)]
    supports = [e for e in active if e.relationship == EdgeType.supports]

    checks: List[DeterministicCheck] = []

    # 1) shared evidence across divergent branches — weakens the independence of a
    #    comparison when two branches lean on the same result.
    evidence_to_concls: Dict[str, List[str]] = {}
    concl_ids = {c.id for c in conclusions}
    for e in supports:
        if e.target_id in concl_ids:
            evidence_to_concls.setdefault(e.source_id, []).append(e.target_id)
    shared: List[str] = []
    for ev, targets in evidence_to_concls.items():
        uniq = list(dict.fromkeys(targets))
        divergent = any(
            not _same_line(by_id, uniq[i], uniq[j])
            for i in range(len(uniq)) for j in range(i + 1, len(uniq))
        )
        if divergent:
            shared.append(ev)
    if shared:
        checks.append(DeterministicCheck(
            name="shared_evidence", status="warn",
            detail=f"{len(shared)} evidence node(s) are relied on by conclusions on "
                   "divergent branches — those branches are less independent than they "
                   "look; a comparison between them is partly circular.",
            node_ids=shared))

    # 2) orphaned conclusions — no supporting evidence edge at all.
    supported_concls = {e.target_id for e in supports}
    orphans = [c.id for c in conclusions if c.id not in supported_concls]
    if orphans:
        checks.append(DeterministicCheck(
            name="orphan_conclusion", status="warn",
            detail=f"{len(orphans)} conclusion(s) have no supporting-evidence edge — they "
                   "rest on no recorded result.",
            node_ids=orphans))

    # 3) untested hypotheses — a hypothesis with no accepted `tests` edge into it.
    tested_hyp = {e.target_id for e in active if e.relationship == EdgeType.tests}
    untested = [h.id for h in hypotheses if h.id not in tested_hyp]
    if untested:
        checks.append(DeterministicCheck(
            name="untested_hypothesis", status="warn",
            detail=f"{len(untested)} hypothesis/hypotheses were raised but never tested — "
                   "the investigation may have concluded with live alternatives open.",
            node_ids=untested))

    # 4) metric drift — the same metric name defined more than one way.
    metrics = [n for n in nodes if isinstance(n.payload, MetricPayload)]
    by_name: Dict[str, Set[str]] = {}
    metric_nodes: Dict[str, List[str]] = {}
    for m in metrics:
        by_name.setdefault(m.payload.name, set()).add(m.payload.sql.strip())
        metric_nodes.setdefault(m.payload.name, []).append(m.id)
    drifted = [name for name, defs in by_name.items() if len(defs) > 1]
    if drifted:
        ids = [nid for name in drifted for nid in metric_nodes[name]]
        checks.append(DeterministicCheck(
            name="metric_drift", status="warn",
            detail=f"{len(drifted)} metric(s) are defined more than one way "
                   f"({', '.join(drifted)}) — the same name means different things across "
                   "the investigation. Reconcile to one definition.",
            node_ids=ids))

    return checks
