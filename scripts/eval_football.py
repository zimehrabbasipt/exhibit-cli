"""Adversarial eval: run the fresh-model analyst questions through Exhibit on the
full 12-table football data, one session (tests cumulative memory), capturing per
question: path, latency, calls, cost, plan steps (with inline SQL/tool args),
executed SQL, conclusion, errors, chart. Writes /tmp/exhibit_eval.json."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.llm.anthropic_client import AnthropicLLM
from exhibit.models import ConclusionPayload, PlanPayload, SqlPayload, ToolCallPayload
from exhibit.store import db
from exhibit.store import nodes as node_store

MODEL = "claude-opus-4-8"
F = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()

QUESTIONS = [
    "How many games are in the dataset, and what date range and set of competitions do they span?",
    "Which 10 players have the most total goals across all appearances, and how many games did each take to score them?",
    "Show me home vs away win rates and average goals scored per game across the whole dataset — is home advantage real here and how big is it?",
    "For the Premier League (GB1), how has the average attendance and average points-per-game gap between home and away teams changed season by season?",
    "Are teams that spend big net transfer fees actually buying league position, or is the correlation weaker than everyone assumes — compare net_transfer_record against final league finish.",
    "Which players had the biggest gap between their peak market value and their transfer fee — i.e. who got sold for way less than they were worth, and does a pattern emerge by age, position, or selling club?",
    "I want to find 'big-game players' — guys whose goal and assist output is disproportionately higher in matches against top-6 opponents or in knockout/final rounds than in ordinary league games. How would you even define and rank that?",
    "If I'm a mid-table club with limited budget, which league or player profile gives me the best resale-value upside — where should I be shopping to buy low and sell high, and what does the data say about which selling clubs and agents consistently generate the largest valuation gains?",
]


def main():
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = AnthropicLLM(model=MODEL)
    t0 = time.time()
    session = orchestrator.start_investigation(conn, paths, sorted(F.glob("*.csv")), client)
    print(f"loaded {len(session.profiles)} tables in {time.time()-t0:.0f}s", flush=True)

    results = []
    for i, q in enumerate(QUESTIONS, 1):
        c0 = client.usage.calls
        cost0 = client.usage.cost_usd(MODEL)
        ts = time.time()
        try:
            r = orchestrator.run_question(session, q)
            err = None
        except Exception as e:
            r, err = None, repr(e)
        dt = time.time() - ts

        rec = {"n": i, "question": q, "latency_s": round(dt, 1),
               "calls": client.usage.calls - c0,
               "cost_usd": round(client.usage.cost_usd(MODEL) - cost0, 4),
               "path": getattr(r, "path", "?"), "crash": err,
               "plan": [], "executed_sql": [], "tools": [], "errors": [],
               "conclusion": None, "chart": False}
        if r is not None:
            if r.plan_node and isinstance(r.plan_node.payload, PlanPayload):
                for s in r.plan_node.payload.plan.steps:
                    rec["plan"].append({"intent": s.intent, "method": s.method,
                                        "sql": s.sql, "tool": s.tool,
                                        "tool_args": s.tool_args_json})
            if r.conclusion_node and isinstance(r.conclusion_node.payload, ConclusionPayload):
                rec["conclusion"] = r.conclusion_node.payload.conclusion.summary
            rec["chart"] = bool(r.chart_nodes)
            rec["errors"] = [e.error for e in r.error_nodes]
            # executed SQL + tool calls for this turn
            qseq = r.question_node.seq
            for n in node_store.list_by_investigation(conn, session.investigation.id):
                if n.seq <= qseq:
                    continue
                if isinstance(n.payload, SqlPayload):
                    rec["executed_sql"].append(n.payload.query.sql)
                elif isinstance(n.payload, ToolCallPayload):
                    rec["tools"].append({"tool": n.payload.call.tool, "inputs": n.payload.call.inputs})
        results.append(rec)
        print(f"Q{i} [{rec['path']}] {rec['latency_s']}s {rec['calls']}call ${rec['cost_usd']} "
              f"err={bool(rec['errors']) or bool(err)} :: {(rec['conclusion'] or err or '')[:90]}",
              flush=True)

    out = {"total_s": round(time.time() - t0, 0),
           "total_cost_usd": round(client.usage.cost_usd(MODEL), 4),
           "usage": vars(client.usage), "results": results}
    Path("/tmp/exhibit_eval.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nTOTAL {out['total_s']}s ${out['total_cost_usd']} → /tmp/exhibit_eval.json", flush=True)


if __name__ == "__main__":
    main()
