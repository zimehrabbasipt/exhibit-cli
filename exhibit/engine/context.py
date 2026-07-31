"""Build compact investigation context for the LLM.

Follow-up questions should build on what's already been established rather than
recomputing it. The append-only node log holds everything; this module distills
the relevant slice into a small text block fed to the planner and narrator:

  1. recent question → conclusion pairs (the running thread / summary), and
  2. the results already computed for the most recent question (compact previews),
     so a follow-up can reference "the 12 rows" instead of re-deriving them.

Kept compact on purpose (summaries + capped row previews) — this is the
"append log, then compact" idea: the raw log stays on disk; only a small,
relevant digest enters the prompt.
"""

from __future__ import annotations

import sqlite3

from ..models import (
    ConclusionPayload,
    MetricPayload,
    QuestionPayload,
    ResultTable,
    SummaryPayload,
    TablePayload,
    ToolResultPayload,
)
from ..store import nodes as node_store

VERBATIM_TURNS = 3       # most-recent question→conclusion pairs kept verbatim
_MAX_RECENT_RESULTS = 4  # result/tool nodes from the latest question
_MAX_PREVIEW_ROWS = 8


def build_context(
    conn: sqlite3.Connection,
    investigation_id: str,
    from_node_id: str | None = None,
) -> str:
    """Return a compact context string (empty if there's no prior activity).

    Call this BEFORE appending the new question node, so 'the most recent
    question' refers to the previous turn.

    When ``from_node_id`` is given (a branch off an earlier node), context is
    scoped to that node's ancestry — the chain of parents from the fork point up
    to the root — so the branch reasons from its own lineage and is blind to
    sibling branches. Without it, context follows the whole investigation by
    recency (the normal linear-thread case).
    """
    nodes = node_store.list_by_investigation(conn, investigation_id)
    if from_node_id is not None:
        by_id = {n.id: n for n in nodes}
        path: set[str] = set()
        cur: str | None = from_node_id
        while cur is not None and cur in by_id:
            path.add(cur)
            cur = by_id[cur].parent_id
        nodes = [n for n in nodes if n.id in path]
    questions = [n for n in nodes if isinstance(n.payload, QuestionPayload)]
    if not questions:
        return ""

    conclusions = [n for n in nodes if isinstance(n.payload, ConclusionPayload)]
    lines: list[str] = []

    # 0a) defined metrics — the semantic layer. Feeding these means SQL REUSES a
    #     metric's canonical definition instead of re-deriving it (so it can't drift).
    metrics = [n for n in nodes if isinstance(n.payload, MetricPayload)]
    if metrics:
        lines.append("Defined metrics — reuse these exact definitions, do not re-derive:")
        for m in metrics:
            desc = f" — {m.payload.description}" if m.payload.description else ""
            lines.append(f"- {m.payload.name}: {m.payload.sql}{desc}")
        lines.append("")

    # 0) rolling summary of older turns (compaction) — retains early findings
    #    cheaply without re-sending every turn.
    summaries = [n for n in nodes if isinstance(n.payload, SummaryPayload)]
    if summaries:
        lines.append("Summary of earlier turns in this investigation:")
        lines.append(summaries[-1].payload.text)
        lines.append("")

    # 1) most-recent question -> conclusion pairs, kept verbatim
    pairs = []
    for q in questions:
        concl = next((c for c in conclusions if c.seq > q.seq), None)
        if concl and isinstance(concl.payload, ConclusionPayload):
            pairs.append((q.payload.question, concl.payload.conclusion.summary))
    if pairs:
        lines.append("Most recent turns (verbatim):")
        for question, answer in pairs[-VERBATIM_TURNS:]:
            lines.append(f"- Q: {question}\n  A: {answer}")

    # 2) results already computed for the most recent question
    last_q_seq = questions[-1].seq
    recent = [
        n
        for n in nodes
        if n.seq > last_q_seq and isinstance(n.payload, (TablePayload, ToolResultPayload))
    ][:_MAX_RECENT_RESULTS]
    if recent:
        lines.append("")
        lines.append(
            "Results already computed for the most recent question — reference "
            "these directly; do NOT recompute them:"
        )
        for n in recent:
            if isinstance(n.payload, TablePayload):
                lines.append(_preview_table(n.payload.table))
            elif isinstance(n.payload, ToolResultPayload):
                r = n.payload.result
                metrics = ", ".join(f"{k}={v}" for k, v in r.metrics.items())
                lines.append(f"- tool {r.tool}: {r.summary}" + (f" [{metrics}]" if metrics else ""))

    return "\n".join(lines)


def _preview_table(t: ResultTable) -> str:
    head = f"- table [{', '.join(t.columns)}] ({t.row_count} rows):"
    rows = [
        "    " + " | ".join("" if v is None else str(v) for v in row)
        for row in t.rows_preview[:_MAX_PREVIEW_ROWS]
    ]
    if t.row_count > min(len(t.rows_preview), _MAX_PREVIEW_ROWS):
        rows.append(f"    … ({t.row_count} rows total)")
    return "\n".join([head, *rows])
