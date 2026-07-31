"""Investigation-DEPTH eval: a single deepening investigation, not independent questions.

A fresh adversary model (no knowledge of Exhibit internals) plays a senior PL analyst
refining ONE investigation rooted in a finding we've already ground-truthed: PL (GB1)
home advantage inverted in the 2020 COVID season. Each turn the adversary is shown the
running transcript (its own prior asks + Exhibit's actual conclusions) and emits the next
refinement move — drilling, filtering, branching, breaking down, CORRECTING a weak
definition the analyst introduced, and finally asking it to restate/rerun the conclusion.

This stresses the product thesis the football eval did NOT: does a correction propagate,
does scope carry forward, does it reuse prior evidence vs silently recompute, does the
graph thread coherently. Per turn we capture the adversary's intended move + target, and
Exhibit's plan/executed SQL/tools/conclusion/threading/latency/cost. Writes
/tmp/exhibit_refine.json.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import List, Literal

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

from pydantic import BaseModel, Field

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.llm import prompts
from exhibit.llm.anthropic_client import AnthropicLLM
from exhibit.models import ConclusionPayload, PlanPayload, SqlPayload, ToolCallPayload
from exhibit.store import db
from exhibit.store import nodes as node_store

MODEL = "claude-opus-4-8"
F = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()
N_TURNS = 7

# Rooting context handed to the adversary — the validated COVID/PL finding from the
# breadth eval. The adversary drives everything else reactively from here.
ROOT_CONTEXT = (
    "A prior scan of this football dataset found something odd: in the English Premier "
    "League (competition_id 'GB1'), the usual home advantage appeared to INVERT during "
    "the 2020 season — away teams did as well or better than home teams, and average "
    "attendance collapsed (the COVID empty-stadium season). You are now refining THIS "
    "single investigation to understand it properly."
)

ADVERSARY_SYSTEM = f"""You are a demanding senior football analyst refining ONE continuous \
investigation with an AI data-analysis tool. This is NOT a list of independent questions — \
every message you send builds directly on the tool's PREVIOUS answer.

{ROOT_CONTEXT}

Your job each turn: read the tool's latest conclusion and push on its WEAKEST or most \
suspicious part. Be the kind of analyst who refines, corrects, and branches:
- drill into a hypothesis the answer raised,
- apply a filter or EXCLUSION to isolate an effect,
- branch/reframe (e.g. before-vs-after a cutoff),
- break the effect down by a derived dimension,
- and crucially: if the tool invents a weak or lazy DEFINITION for something (e.g. \
"club quality"), CORRECT it and tell it to use a better one instead,
- near the end, ask it to restate / rerun its overall conclusion given everything.

Rules:
- Assume the tool remembers the investigation so far. Refer to prior results and scope \
implicitly ("exclude those", "now split that by...") rather than re-stating everything — \
you are testing whether it actually carries context.
- One concrete analytical move per turn. Sound like a real analyst talking, one or two \
sentences. Do not hedge or explain your meta-strategy to the tool.
- Do not repeat a move you've already made; escalate."""


class AdversaryTurn(BaseModel):
    message: str = Field(description="The exact next message to send the analysis tool, in the analyst's voice.")
    move: Literal[
        "drill", "exclude_filter", "branch_reframe", "breakdown",
        "correct_definition", "rerun_restate", "other",
    ] = Field(description="The refinement move this message makes.")
    targets: str = Field(description="Which specific part of the tool's PRIOR answer (a claim, a definition, a scope) this move acts on. 'seed' for the first turn.")


def ask_adversary(client: AnthropicLLM, transcript: List[dict]) -> AdversaryTurn:
    """Generate the next refinement turn from the running transcript."""
    if not transcript:
        user = ("Start the investigation. Ask your first question about the inverted "
                "home advantage in the 2020 Premier League season.")
    else:
        lines = []
        for t in transcript:
            lines.append(f"[Turn {t['n']}] You asked: {t['question']}")
            lines.append(f"           Tool concluded: {t['conclusion']}")
        user = ("Investigation so far:\n\n" + "\n".join(lines) +
                "\n\nWrite your next message, refining or correcting based on the tool's "
                "last answer.")
    response = client._client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=ADVERSARY_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=AdversaryTurn,
    )
    client.usage.record(response.usage)  # accumulate adversary token usage
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(f"adversary returned no structured output (stop={getattr(response,'stop_reason','?')})")
    return parsed


def main():
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    tool_client = AnthropicLLM(model=MODEL)   # the system under test
    adv_client = AnthropicLLM(model=MODEL)    # the fresh adversary (separate usage)

    t0 = time.time()
    session = orchestrator.start_investigation(conn, paths, sorted(F.glob("*.csv")), tool_client)
    print(f"loaded {len(session.profiles)} tables in {time.time()-t0:.0f}s", flush=True)

    transcript: List[dict] = []
    results = []
    prev_conclusion_id = None

    for i in range(1, N_TURNS + 1):
        turn = ask_adversary(adv_client, transcript)
        q = turn.message
        print(f"\n--- Turn {i} [{turn.move}] targets={turn.targets!r}\n    Q: {q}", flush=True)

        c0 = tool_client.usage.calls
        cost0 = tool_client.usage.cost_usd(MODEL)
        ts = time.time()
        try:
            r = orchestrator.run_question(session, q)
            err = None
        except Exception as e:
            r, err = None, repr(e)
        dt = time.time() - ts

        rec = {
            "n": i, "move": turn.move, "targets": turn.targets, "question": q,
            "latency_s": round(dt, 1),
            "calls": tool_client.usage.calls - c0,
            "cost_usd": round(tool_client.usage.cost_usd(MODEL) - cost0, 4),
            "path": getattr(r, "path", "?"), "crash": err,
            "plan": [], "executed_sql": [], "tools": [], "errors": [],
            "conclusion": None, "chart": False,
            # threading: did this question attach under the prior conclusion (a coherent
            # deepening chain)?
            "q_parent_id": None, "q_seq": None,
            "threaded_under_prior_conclusion": None,
        }
        if r is not None:
            rec["q_parent_id"] = r.question_node.parent_id
            rec["q_seq"] = r.question_node.seq
            rec["threaded_under_prior_conclusion"] = (
                prev_conclusion_id is not None and r.question_node.parent_id == prev_conclusion_id
            )
            if r.plan_node and isinstance(r.plan_node.payload, PlanPayload):
                for s in r.plan_node.payload.plan.steps:
                    rec["plan"].append({"intent": s.intent, "method": s.method,
                                        "sql": s.sql, "tool": s.tool,
                                        "tool_args": s.tool_args_json})
            if r.conclusion_node and isinstance(r.conclusion_node.payload, ConclusionPayload):
                rec["conclusion"] = r.conclusion_node.payload.conclusion.summary
                prev_conclusion_id = r.conclusion_node.id
            rec["chart"] = bool(r.chart_nodes)
            rec["errors"] = [e.error for e in r.error_nodes]
            qseq = r.question_node.seq
            for n in node_store.list_by_investigation(conn, session.investigation.id):
                if n.seq <= qseq:
                    continue
                if isinstance(n.payload, SqlPayload):
                    rec["executed_sql"].append(n.payload.query.sql)
                elif isinstance(n.payload, ToolCallPayload):
                    rec["tools"].append({"tool": n.payload.call.tool, "inputs": n.payload.call.inputs})

        results.append(rec)
        transcript.append({"n": i, "question": q, "conclusion": rec["conclusion"] or err or "(no conclusion)"})
        print(f"    [{rec['path']}] {rec['latency_s']}s {rec['calls']}call ${rec['cost_usd']} "
              f"thread_ok={rec['threaded_under_prior_conclusion']} err={bool(rec['errors']) or bool(err)}"
              f"\n    A: {(rec['conclusion'] or err or '')[:200]}", flush=True)

    out = {
        "total_s": round(time.time() - t0, 0),
        "tool_cost_usd": round(tool_client.usage.cost_usd(MODEL), 4),
        "adversary_cost_usd": round(adv_client.usage.cost_usd(MODEL), 4),
        "tool_usage": vars(tool_client.usage),
        "adversary_usage": vars(adv_client.usage),
        "results": results,
    }
    Path("/tmp/exhibit_refine.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nTOTAL {out['total_s']}s  tool=${out['tool_cost_usd']} "
          f"adversary=${out['adversary_cost_usd']} → /tmp/exhibit_refine.json", flush=True)


if __name__ == "__main__":
    main()
