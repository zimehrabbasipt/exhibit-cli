"""Head-to-head: Exhibit vs a naive LLM agent on the 12-table football dataset.

Same 6 questions, asked strictly one at a time, two ways — reporting time /
tokens / cost:

  1. Exhibit  — structured investigation (plan -> SQL/tools over DuckDB -> narrate)
               with prompt caching + rolling-summary compaction + step reuse.
  2. Baseline — a "normal" LLM agent: given only the schema (no data), it WRITES
               SQL via a run_sql tool, iterates, and answers. One growing
               conversation across the 6 questions, no prompt caching, no
               compaction (i.e. what you get without Exhibit's engineering).

Both hit the same read-only DuckDB, so data access is identical; the difference
is the scaffolding. Also notes why dumping the raw data in the prompt is
infeasible at this scale.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

import anthropic

from exhibit.config import AppPaths
from exhibit.data import loader
from exhibit.engine import orchestrator
from exhibit.llm import prompts
from exhibit.llm.anthropic_client import AnthropicLLM
from exhibit.store import db

MODEL = "claude-opus-4-8"
FOOTBALL = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()
FILES = sorted(FOOTBALL.glob("*.csv"))
MAX_TOOL_ITERS = 6  # per question, baseline agent

QUESTIONS = [
    "Which clubs got the best value from incoming transfers — most goals+assists "
    "produced per euro of transfer fee paid?",
    "How has total player market value changed year over year across the dataset?",
    "Which agents represent the highest total current market value of players?",
    "Do clubs with higher incoming transfer spend score more goals overall?",
    "Which countries produce players with the highest average market value?",
    "What is the relationship between player age and market value?",
]


def _p(m: str) -> None:
    print(m, flush=True)


# --------------------------------------------------------------------------- #
# Exhibit
# --------------------------------------------------------------------------- #

def run_exhibit():
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = AnthropicLLM(model=MODEL)
    t0 = time.time()
    session = orchestrator.start_investigation(conn, paths, FILES, client)
    load_t = time.time() - t0
    _p(f"[exhibit] loaded {len(session.profiles)} tables in {load_t:.0f}s")
    for i, q in enumerate(QUESTIONS, 1):
        ts = time.time()
        try:
            r = orchestrator.run_question(session, q)
            ok = r.conclusion_node is not None
            note = r.conclusion_node.payload.conclusion.summary[:110] if ok else "(no conclusion)"
        except Exception as e:
            ok, note = False, f"ERROR: {e}"
        _p(f"[exhibit] Q{i} {time.time()-ts:.0f}s ok={ok} :: {note}")
    total = time.time() - t0
    return {"load_t": load_t, "total": total, "usage": session.client.usage,
            "catalog": prompts.format_catalog(session.profiles),
            "duckdb_path": session.duckdb_path}


# --------------------------------------------------------------------------- #
# Baseline: naive LLM agent that writes SQL (growing chat, no caching)
# --------------------------------------------------------------------------- #

def _run_sql(duck, query: str) -> str:
    try:
        cur = duck.execute(query)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(60)
        lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
        text = "\n".join(lines)
        return text[:3000] + ("\n… (truncated)" if len(text) > 3000 else "")
    except Exception as e:
        return f"ERROR: {e}"


def run_baseline(catalog: str, duckdb_path):
    client = anthropic.Anthropic()
    duck = loader.open_readonly(duckdb_path)
    tools = [{
        "name": "run_sql",
        "description": "Run a read-only DuckDB SQL query over the tables; returns rows.",
        "input_schema": {"type": "object",
                         "properties": {"query": {"type": "string"}},
                         "required": ["query"]},
    }]
    system = [{"type": "text", "text":
               "You are a data analyst. You have a run_sql tool over these tables "
               "(you do NOT have the raw data otherwise). Write SQL to compute the "
               "answer, iterate if needed, then answer concisely.\n\n" + catalog}]

    messages = []  # one growing conversation across all questions (naive)
    tin = tout = 0
    t0 = time.time()
    for i, q in enumerate(QUESTIONS, 1):
        ts = time.time()
        messages.append({"role": "user", "content": q})
        for _ in range(MAX_TOOL_ITERS):
            resp = client.messages.create(
                model=MODEL, max_tokens=4096, thinking={"type": "adaptive"},
                system=system, tools=tools, messages=messages,
            )
            tin += resp.usage.input_tokens
            tout += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "tool_use":
                results = []
                for blk in resp.content:
                    if getattr(blk, "type", None) == "tool_use" and blk.name == "run_sql":
                        results.append({"type": "tool_result", "tool_use_id": blk.id,
                                        "content": _run_sql(duck, blk.input.get("query", ""))})
                messages.append({"role": "user", "content": results})
                continue
            break
        ans = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        _p(f"[baseline] Q{i} {time.time()-ts:.0f}s :: {ans[:110]}")
    return {"total": time.time() - t0, "in": tin, "out": tout}


def main():
    _p(f"files ({len(FILES)}): {[f.name for f in FILES]}")
    a = run_exhibit()
    b = run_baseline(a["catalog"], a["duckdb_path"])

    u = a["usage"]
    exhibit_cost = u.cost_usd(MODEL)
    exhibit_uncached = u.uncached_cost_usd(MODEL)
    base_cost = (b["in"] * 5 + b["out"] * 25) / 1_000_000
    total_bytes = sum(f.stat().st_size for f in FILES)
    est_tokens = total_bytes / 4

    _p("\n================ RESULTS (6 questions, 12 tables) ================")
    _p(f"Exhibit    time {a['total']:.0f}s (load {a['load_t']:.0f}s + 6Q {a['total']-a['load_t']:.0f}s)")
    _p(f"          {u.calls} calls | in {u.input_tokens:,} · cache_r {u.cache_read_tokens:,} · "
       f"cache_w {u.cache_write_tokens:,} · out {u.output_tokens:,}")
    _p(f"          cost ${exhibit_cost:.4f}  (uncached ${exhibit_uncached:.4f}; caching saved "
       f"${exhibit_uncached-exhibit_cost:.4f})")
    _p(f"Baseline  time {b['total']:.0f}s | in {b['in']:,} · out {b['out']:,} (no caching) | "
       f"cost ${base_cost:.4f}")
    _p(f"\nRatio     baseline / Exhibit  cost {base_cost/exhibit_cost:.2f}x  "
       f"input-tokens {b['in']/max(1,u.total_input_tokens):.2f}x")
    _p(f"Data-in-prompt baseline: {total_bytes/1e6:.0f} MB ≈ {est_tokens/1e6:.0f}M tokens "
       f"(~{est_tokens/1e6:.0f}x the 1M window) ⇒ infeasible; ~${est_tokens*5/1e6:.0f}/call if it fit")


if __name__ == "__main__":
    main()
