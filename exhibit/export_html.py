"""Render an investigation to a self-contained, dark, interactive HTML viewer.

Same append-only graph as the Markdown export, presented like the design mockup: a
dark theme, clickable branch chips that switch the view to a branch (its ancestry +
its own turns), collapsible question cards, evidence chips that anchor to collapsible
evidence-chain sections, inlined charts re-rendered with the dark palette, and a
judge-caveats panel from the deterministic graph checks + materialized hypotheses.

One file: ships its own CSS + a few lines of vanilla JS for branch switching; no
external assets, no server. **No LLM calls** — chart re-rendering is matplotlib over
the stored result table (deterministic); everything else is a projection of the graph.
"""

from __future__ import annotations

import base64
import html
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from .engine import charts, graph
from .models import (
    ChartPayload,
    ConclusionPayload,
    HypothesisPayload,
    Investigation,
    MetricPayload,
    Node,
    NodeKind,
    PlanPayload,
    QuestionPayload,
    SqlPayload,
    TablePayload,
    ToolCallPayload,
    ToolResultPayload,
)
from .store import nodes as node_store

_CONF = {"high": ("#12271f", "#3ed6a1"), "medium": ("#2a2113", "#ffcb5c"),
         "low": ("#2a1614", "#ff8f5e")}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _sid(node_id: str) -> str:
    return node_id[:4]


def _short(text: str, n: int = 30) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_STYLE = """
:root{
  --surface-0:#0f0f12; --surface-1:#16161a; --surface-2:#1c1c21; --surface-3:#101014;
  --border:#2b2b31; --text-primary:#f1f1ef; --text-secondary:#b9b8b3; --text-muted:#7f7e78;
  --accent:#5b9cf0; --evidence-bg:rgba(62,214,161,.14); --evidence-fg:#57dda9;
  --branch-bg:#211f3a; --branch-fg:#b9b0ff;
  --font-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-0);color:var(--text-primary);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:840px;margin:28px auto;padding:0 16px;}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:4px 4px 14px;}
.title{margin:0;font-size:18px;font-weight:600;}
.sub{margin:4px 0 0;font-size:12.5px;color:var(--text-secondary);}
.pill{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--surface-2);
  border:0.5px solid var(--border);color:var(--text-secondary);}
.bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:0 4px 16px;font-size:12px;}
.bar .lbl{color:var(--text-muted);margin-right:2px;}
.chip{padding:4px 11px;border-radius:99px;background:var(--surface-2);border:0.5px solid var(--border);
  color:var(--text-secondary);cursor:pointer;font-size:12px;font-family:inherit;}
.chip.on{background:var(--branch-bg);color:var(--branch-fg);border-color:transparent;font-weight:500;}
.card{background:var(--surface-2);border:0.5px solid var(--border);border-radius:12px;
  padding:.6rem 1.15rem;margin-bottom:12px;}
details.card>summary{list-style:none;cursor:pointer;padding:.4rem 0;}
details.card>summary::-webkit-details-marker{display:none}
.meta{display:flex;justify-content:space-between;align-items:center;gap:8px;
  font-size:12px;color:var(--text-muted);}
.conf{font-size:11.5px;padding:3px 9px;border-radius:99px;font-weight:500;}
.q{margin:6px 0 0;font-size:14.5px;color:var(--text-secondary);font-style:italic;}
.body{padding:2px 0 8px;}
.a{margin:10px 0 12px;font-size:15px;}
.ev{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.ev .lbl{font-size:12px;color:var(--text-muted);}
.ev a{font-size:12px;padding:3px 9px;border-radius:6px;background:var(--evidence-bg);
  color:var(--evidence-fg);font-family:var(--font-mono);text-decoration:none;}
.chain{background:var(--surface-1);border:0.5px solid var(--border);border-radius:10px;
  padding:.7rem .9rem;margin-top:10px;}
.chain .hd{font-size:12.5px;color:var(--text-muted);margin-bottom:6px;}
details.sub{background:var(--surface-3);border:0.5px solid var(--border);border-radius:8px;
  padding:8px 12px;margin:8px 0;}
details.sub>summary{cursor:pointer;font-size:12.5px;color:var(--text-secondary);
  font-family:var(--font-mono);}
pre{font-family:var(--font-mono);font-size:12px;line-height:1.6;color:var(--text-secondary);
  overflow-x:auto;margin:8px 0 0;white-space:pre-wrap;}
table{width:100%;font-size:12.5px;border-collapse:collapse;margin-top:8px;}
th{text-align:left;font-weight:500;color:var(--text-muted);padding:4px 8px;}
td{padding:4px 8px;border-top:0.5px solid var(--border);color:var(--text-secondary);}
.rows{margin:8px 0 0;font-size:11.5px;color:var(--text-muted);}
figure{margin:10px 0 0}
figure img{max-width:100%;border:0.5px solid var(--border);border-radius:8px;display:block}
figcaption{margin-top:4px;font-size:11.5px;color:var(--text-muted);}
.caveats{background:#211808;border:0.5px solid #3a2c10;border-radius:12px;padding:.9rem 1.15rem;margin-bottom:14px;}
.caveats h3{margin:0 0 8px;font-size:13px;font-weight:600;color:#ffcb5c;}
.caveats p{margin:0 0 6px;font-size:13px;line-height:1.5;color:#e0b877;}
.caveats .ref{font-family:var(--font-mono);font-size:11.5px;opacity:.85;}
.branchtag{font-size:11px;color:var(--branch-fg);background:var(--branch-bg);border-radius:99px;padding:2px 8px;}
.foot{padding:6px 4px 0;font-size:12px;color:var(--text-muted);}
.anchor{scroll-margin-top:16px;}
"""

_JS = """
function showBranch(b){
  var s=String(b);
  document.querySelectorAll('.turn').forEach(function(el){
    var bs=(el.getAttribute('data-branches')||'').split(' ');
    el.style.display = bs.indexOf(s)>=0 ? '' : 'none';
  });
  document.querySelectorAll('.chip[data-b]').forEach(function(c){
    c.classList.toggle('on', c.getAttribute('data-b')===s);
  });
}
document.addEventListener('DOMContentLoaded', function(){ showBranch(0); });
"""


class _Turn:
    def __init__(self, q: Node):
        self.q = q
        self.sql: List[Node] = []
        self.tables: List[Node] = []
        self.tools: List[Node] = []
        self.conclusion: Optional[Node] = None
        self.charts: List[Node] = []
        self.errors: List[Node] = []


def _turns(nodes: List[Node]) -> List[_Turn]:
    turns: List[_Turn] = []
    cur: Optional[_Turn] = None
    for n in nodes:
        p = n.payload
        if isinstance(p, QuestionPayload):
            cur = _Turn(n)
            turns.append(cur)
        elif cur is None:
            continue
        elif isinstance(p, SqlPayload):
            cur.sql.append(n)
        elif isinstance(p, TablePayload):
            cur.tables.append(n)
        elif isinstance(p, (ToolCallPayload, ToolResultPayload)):
            cur.tools.append(n)
        elif isinstance(p, ConclusionPayload):
            cur.conclusion = n
        elif isinstance(p, ChartPayload):
            cur.charts.append(n)
        elif n.kind == NodeKind.error:
            cur.errors.append(n)
    return turns


# --- branches (for the clickable switcher) --------------------------------- #

def _branches(turns: List[_Turn]):
    """Return (labels: {id->str}, visible: {turn_id->set(branch ids)}). Branch 0 is the
    main thread (follow first-children from the root); each additional child of a
    conclusion starts a new branch; a branch is visible together with its ancestry."""
    concl_turn = {t.conclusion.id: t for t in turns if t.conclusion}
    children: Dict[str, List[_Turn]] = {}
    roots: List[_Turn] = []
    for t in turns:
        pid = t.q.parent_id
        (children.setdefault(pid, []).append(t) if pid in concl_turn else roots.append(t))

    labels = {0: "main thread"}
    branch_of: Dict[str, int] = {}
    nxt = [1]

    def walk(turn: _Turn, bid: int):
        branch_of[turn.q.id] = bid
        kids = children.get(turn.conclusion.id, []) if turn.conclusion else []
        for i, k in enumerate(kids):
            if i == 0:
                walk(k, bid)
            else:
                b = nxt[0]; nxt[0] += 1
                labels[b] = _short(k.q.payload.question)
                walk(k, b)

    for r in roots:
        walk(r, 0)

    visible: Dict[str, set] = {t.q.id: {branch_of.get(t.q.id, 0)} for t in turns}
    for t in turns:
        b = branch_of.get(t.q.id, 0)
        if b == 0:
            continue
        pid = t.q.parent_id            # climb ancestry, marking it visible for branch b
        while pid in concl_turn:
            anc = concl_turn[pid]
            visible[anc.q.id].add(b)
            pid = anc.q.parent_id
    return labels, visible


# --- rendering ------------------------------------------------------------- #

def _table_preview(t) -> str:
    cols = "".join(f"<th>{_e(c)}</th>" for c in t.columns)
    rows = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>"
                   for r in t.rows_preview[:5])
    shown = min(len(t.rows_preview), 5)
    more = (f'<p class="rows">{shown} of {t.row_count:,} rows shown · full result stored'
            f'</p>') if t.row_count > shown else ""
    return f"<table><tr>{cols}</tr>{rows}</table>{more}"


def _chart_img(node: Node, by_id: Dict[str, Node]) -> str:
    """Inline a chart. Re-render it dark from the stored result table (deterministic,
    matplotlib — no LLM); fall back to the stored light PNG if that isn't possible."""
    p = node.payload
    data = None
    src = by_id.get(getattr(p, "source_node_id", None) or "")
    if isinstance(p, ChartPayload) and src and isinstance(src.payload, TablePayload):
        try:
            tmp = Path(tempfile.mkstemp(suffix=".png")[1])
            charts.render_png(src.payload.table, p.chart_type, p.x, p.y, p.title, tmp, dark=True)
            data = base64.b64encode(tmp.read_bytes()).decode("ascii")
            tmp.unlink()
        except Exception:
            data = None
    if data is None:  # fallback: the stored (light) artifact
        path = getattr(node, "artifact_path", None)
        if path and Path(path).exists():
            try:
                data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            except Exception:
                return ""
        else:
            return ""
    title = _e(p.title) if isinstance(p, ChartPayload) else "chart"
    return (f'<figure><img alt="{title}" src="data:image/png;base64,{data}">'
            f'<figcaption>{title}</figcaption></figure>')


def _evidence_chain(turn: _Turn, by_id: Dict[str, Node]) -> str:
    if not (turn.sql or turn.tools or turn.charts):
        return ""
    out = [f'<div class="chain"><div class="hd">Evidence chain — '
           f'{len(turn.sql) + len(turn.tools)} step(s) · rerun-safe</div>']
    tbl_by_parent = {t.parent_id: t for t in turn.tables}
    for s in turn.sql:
        sp: SqlPayload = s.payload
        out.append(f'<div class="anchor" id="n-{_sid(s.id)}">'
                   f'<details class="sub" open><summary>sql:{_sid(s.id)} — '
                   f'{_e(sp.query.notes or "query")}</summary><pre>{_e(sp.query.sql)}</pre></details>')
        tbl = tbl_by_parent.get(s.id)
        if tbl and isinstance(tbl.payload, TablePayload):
            out.append(f'<div class="anchor" id="n-{_sid(tbl.id)}">{_table_preview(tbl.payload.table)}</div>')
        out.append("</div>")
    for tl in turn.tools:
        if isinstance(tl.payload, ToolResultPayload):
            r = tl.payload.result
            metrics = ", ".join(f"{k}={v}" for k, v in r.metrics.items())
            out.append(f'<div class="anchor" id="n-{_sid(tl.id)}"><details class="sub"><summary>'
                       f'tool:{_e(r.tool)}:{_sid(tl.id)}</summary><pre>{_e(r.summary)}'
                       f'{("  [" + _e(metrics) + "]") if metrics else ""}</pre></details></div>')
    for ch in turn.charts:
        out.append(_chart_img(ch, by_id))
    out.append("</div>")
    return "".join(out)


def _turn_card(turn: _Turn, by_id: Dict[str, Node], visible_ids: set) -> str:
    q = turn.q.payload.question
    conf_pill = ""
    if turn.conclusion and isinstance(turn.conclusion.payload, ConclusionPayload):
        c = turn.conclusion.payload.conclusion
        bg, fg = _CONF.get(c.confidence, ("#222", "#ccc"))
        conf_pill = f'<span class="conf" style="background:{bg};color:{fg}">{c.confidence}</span>'
    branches_attr = " ".join(str(b) for b in sorted(visible_ids))

    body = []
    if turn.conclusion and isinstance(turn.conclusion.payload, ConclusionPayload):
        c = turn.conclusion.payload.conclusion
        body.append(f'<p class="a">{_e(c.summary)}</p>')
        if c.evidence_node_ids:
            chips = "".join(f'<a href="#n-{_sid(n)}">ev:{_sid(n)}</a>' for n in c.evidence_node_ids)
            body.append(f'<div class="ev"><span class="lbl">Evidence:</span>{chips}</div>')
    for err in turn.errors:
        body.append(f'<p class="q" style="color:#ff8f5e">⚠ {_e(getattr(err.payload, "message", ""))}</p>')
    body.append(_evidence_chain(turn, by_id))

    return (f'<div class="turn" data-branches="{branches_attr}">'
            f'<details class="card" open><summary>'
            f'<div class="meta"><span>Q · {_e(turn.q.created_at[:10])}</span>{conf_pill}</div>'
            f'<p class="q">"{_e(q)}"</p></summary>'
            f'<div class="body">{"".join(body)}</div></details></div>')


def _caveats(conn: sqlite3.Connection, inv_id: str, nodes: List[Node]) -> str:
    items: List[str] = []
    for c in graph.graph_lint(conn, inv_id):
        ids = " · ".join(_sid(n) for n in c.node_ids)
        items.append(f'<p>{_e(c.detail)} <span class="ref">{_e(c.name)}'
                     f'{(" · " + ids) if ids else ""}</span></p>')
    for n in nodes:
        if isinstance(n.payload, HypothesisPayload):
            st = graph.hypothesis_status(conn, n.id)
            if st in ("proposed", "unresolved"):
                items.append(f'<p>Untested alternative: {_e(n.payload.statement)} '
                             f'<span class="ref">hyp:{_sid(n.id)} · {st}</span></p>')
    if not items:
        return ""
    return (f'<div class="caveats"><h3>Judge caveats — {len(items)} open</h3>'
            + "".join(items) + "</div>")


def export_html(conn: sqlite3.Connection, investigation: Investigation) -> str:
    nodes = node_store.list_by_investigation(conn, investigation.id)
    by_id = {n.id: n for n in nodes}
    turns = _turns(nodes)
    labels, visible = _branches(turns)
    metrics = [n for n in nodes if isinstance(n.payload, MetricPayload)]

    # snapshot provenance from any profile node that carries it
    snap = ""
    for n in nodes:
        pr = getattr(n.payload, "profile", None)
        if pr is not None and getattr(pr, "source", None):
            cap = f" (capped from {pr.source_row_count:,})" if pr.truncated else ""
            snap = (f' · <span style="color:var(--branch-fg)">snapshot of '
                    f'{_e(pr.source)} · {_e((pr.snapshot_at or "")[:16])} · '
                    f'{pr.row_count:,} rows{cap}</span>')
            break

    body = [f'<div class="head"><div>'
            f'<p class="title">{_e(investigation.name)}</p>'
            f'<p class="sub">{_e(investigation.table_name)} · {len(nodes)} nodes · '
            f'created {_e(investigation.created_at[:10])}{snap}</p></div>'
            f'<div style="display:flex;gap:8px;flex-shrink:0">'
            f'<span class="pill">{len(turns)} questions</span>'
            f'<span class="pill">{max(len(labels) - 1, 0)} branch(es)</span></div></div>']

    # branch switcher
    chips = "".join(
        f'<button class="chip" data-b="{b}" onclick="showBranch({b})">{_e(lbl)}</button>'
        for b, lbl in sorted(labels.items()))
    body.append(f'<div class="bar"><span class="lbl">Branches:</span>{chips}</div>')

    if metrics:
        mchips = "".join(f'<span class="pill">{_e(m.payload.name)}</span>' for m in metrics)
        body.append(f'<div class="bar"><span class="lbl">Metrics:</span>{mchips}</div>')

    body.append(_caveats(conn, investigation.id, nodes))

    for t in turns:
        body.append(_turn_card(t, by_id, visible.get(t.q.id, {0})))

    body.append('<p class="foot">Generated by Exhibit · deterministic · '
                'resume with <code>exhibit open</code></p>')

    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(investigation.name)} · Exhibit</title><style>{_STYLE}</style></head>"
            f"<body><div class='wrap'>{''.join(body)}</div><script>{_JS}</script></body></html>")
