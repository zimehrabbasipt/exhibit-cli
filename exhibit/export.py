"""Render an investigation to a Markdown report.

The export walks the append-only node log in order and formats each typed node.
Because everything the system did is a persisted node, the report is a faithful,
reproducible record — not a re-summarization.
"""

from __future__ import annotations

import sqlite3
from typing import List

from .models import (
    ConclusionPayload,
    CritiquePayload,
    ErrorPayload,
    FollowUpPayload,
    HypothesisPayload,
    MetricPayload,
    Investigation,
    InterpretationPayload,
    Node,
    PlanPayload,
    ChartPayload,
    ProfilePayload,
    QuestionPayload,
    SqlPayload,
    SummaryPayload,
    TablePayload,
    ToolCallPayload,
    ToolResultPayload,
)
from .store import nodes as node_store


def export_markdown(conn: sqlite3.Connection, investigation: Investigation) -> str:
    nodes = node_store.list_by_investigation(conn, investigation.id)
    lines: List[str] = []
    lines.append(f"# Investigation: {investigation.name}")
    lines.append("")
    lines.append(f"- Created: {investigation.created_at}")
    lines.append(f"- Data file: `{investigation.data_path}` ({investigation.data_format})")
    lines.append(f"- Table: `{investigation.table_name}`")
    lines.append("")

    for node in nodes:
        lines.extend(_render_node(node))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_node(node: Node) -> List[str]:
    p = node.payload
    sid = node.id[:8]
    header = f"## [{node.seq}] {node.kind.value} · `{sid}`"
    out = [header, ""]

    if isinstance(p, ProfilePayload):
        prof = p.profile
        out.append(f"**{prof.row_count:,} rows · {len(prof.columns)} columns**")
        out.append("")
        out.append("| column | type | null % | distinct | min | max |")
        out.append("| --- | --- | ---: | ---: | --- | --- |")
        for c in prof.columns:
            out.append(
                f"| {c.name} | {c.dtype} | {c.null_fraction * 100:.1f}% | "
                f"{c.distinct_count if c.distinct_count is not None else ''} | "
                f"{c.min or ''} | {c.max or ''} |"
            )
    elif isinstance(p, QuestionPayload):
        out.append(f"**Q:** {p.question}")
    elif isinstance(p, PlanPayload):
        out.append(f"_{p.plan.rationale}_")
        out.append("")
        for i, step in enumerate(p.plan.steps, 1):
            out.append(f"{i}. **{step.intent}** — {step.description}")
    elif isinstance(p, SqlPayload):
        out.append("```sql")
        out.append(p.query.sql)
        out.append("```")
        if p.query.notes:
            out.append(f"_{p.query.notes}_")
    elif isinstance(p, TablePayload):
        out.extend(_render_table(p))
        if node.artifact_path:
            out.append("")
            out.append(f"_Full result: `{node.artifact_path}`_")
    elif isinstance(p, ToolCallPayload):
        args = ", ".join(f"{k}={v}" for k, v in p.call.inputs.items())
        out.append(f"**Tool:** `{p.call.tool}({args})`")
    elif isinstance(p, ToolResultPayload):
        r = p.result
        out.append(r.summary)
        if r.metrics:
            out.append("")
            out.append("| metric | value |")
            out.append("| --- | --- |")
            for k, v in r.metrics.items():
                out.append(f"| {k} | {v} |")
        for c in r.caveats:
            out.append(f"> ⚠️ {c}")
    elif isinstance(p, InterpretationPayload):
        for f in p.interpretation.findings:
            out.append(f"- {f}")
    elif isinstance(p, ConclusionPayload):
        out.append(f"**Conclusion ({p.conclusion.confidence} confidence):** {p.conclusion.summary}")
    elif isinstance(p, FollowUpPayload):
        out.append(f"- **{p.follow_up.question}** — {p.follow_up.why}")
    elif isinstance(p, ChartPayload):
        out.append(f"**Chart:** {p.title} _({p.chart_type})_")
        if node.artifact_path:
            out.append("")
            out.append(f"![{p.title}]({node.artifact_path})")
    elif isinstance(p, SummaryPayload):
        out.append(f"_Running summary:_ {p.text}")
    elif isinstance(p, HypothesisPayload):
        out.append(f"**Hypothesis** ({p.origin}): {p.statement}")
    elif isinstance(p, MetricPayload):
        out.append(f"**Metric** `{p.name}` = `{p.sql}`"
                   + (f" — {p.description}" if p.description else ""))
    elif isinstance(p, CritiquePayload):
        out.extend(_render_critique(p))
    elif isinstance(p, ErrorPayload):
        out.append(f"> ⚠️ Error in {p.stage}: {p.message}")
    return out


def _render_critique(p: CritiquePayload) -> List[str]:
    out: List[str] = []
    if p.mode == "lint":
        warns = [c for c in p.checks if c.status == "warn"]
        if not warns:
            out.append(f"_Self-check: {len(p.checks)} checks passed._")
            return out
        out.append(f"**Self-check — {len(warns)} warning(s):**")
        for c in warns:
            out.append(f"- ⚠️ {c.detail} _({c.name})_")
        return out
    r = p.review
    if r is None:
        return out
    out.append(f"**Review:** {r.overall}")
    if getattr(r, "claims", None):
        out.append("")
        out.append("_Claims (conclusion decomposed):_")
        mark = {"supported": "✅", "weak": "⚠️", "unsupported": "❌"}
        for c in r.claims:
            out.append(f"- {mark.get(c.verdict, '•')} _{c.claim_type}_ — {c.text} ({c.why})")
    if r.weak_conclusions:
        out.append("")
        out.append("_Weakly supported:_")
        for w in r.weak_conclusions:
            out.append(f"- {w.claim} — {w.why}")
    if r.untested_alternatives:
        out.append("")
        out.append("_Untested alternatives:_")
        for a in r.untested_alternatives:
            out.append(f"- {a.hypothesis} — test: {a.how_to_test}")
    if r.assumptions:
        out.append("")
        out.append("_Assumptions:_ " + "; ".join(r.assumptions))
    if r.missing_evidence:
        out.append("")
        out.append("_Missing evidence:_ " + "; ".join(r.missing_evidence))
    if r.simpler_explanation:
        out.append("")
        out.append(f"_Simpler explanation:_ {r.simpler_explanation}")
    if r.confidence_assessment:
        out.append("")
        out.append(f"_Confidence:_ {r.confidence_assessment}")
    out.append("")
    out.append(f"_Highest-value next query:_ **{r.next_query.question}** — {r.next_query.why}")
    return out


def _render_table(p: TablePayload, max_rows: int = 20) -> List[str]:
    t = p.table
    if not t.columns:
        return ["_(no columns)_"]
    out = ["| " + " | ".join(t.columns) + " |"]
    out.append("| " + " | ".join("---" for _ in t.columns) + " |")
    for row in t.rows_preview[:max_rows]:
        out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    if t.row_count > min(len(t.rows_preview), max_rows):
        out.append("")
        out.append(f"_… {t.row_count:,} rows total_")
    return out
