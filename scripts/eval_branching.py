"""Investigation-DEPTH + BRANCHING eval: one investigation with real graph forks.

Like eval_refinement.py, a fresh adversary model drives ONE deepening investigation
reactively (shown Exhibit's actual conclusion each turn). The difference: the adversary
can BRANCH — fork a new question off an EARLIER turn's node instead of the latest one —
so we exercise the `/branch` capability (run_question(parent_id=...)) and its
ancestry-scoped context. It is explicitly asked to, at least once, spawn two sibling
branches of competing explanations from the same node and later compare them.

Rooted in a natural experiment analogous to COVID: Newcastle United's Oct-2021 takeover,
a discrete exogenous shock with an obvious before/after and confounds (manager, spending,
opponent quality) to rule out. Every dependent number is verified independently in DuckDB.
Writes /tmp/exhibit_branch.json.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import List, Literal, Optional

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

from pydantic import BaseModel, Field

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.models import ConclusionPayload, PlanPayload, SqlPayload, ToolCallPayload
from exhibit.store import db
from exhibit.store import nodes as node_store

MODEL = "claude-opus-4-8"
F = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()
N_TURNS = 8

ROOT_CONTEXT = (
    "A prior scan of this football dataset surfaced a natural experiment: Newcastle United "
    "(English Premier League, competition_id 'GB1') spent a decade as a mid-to-lower-table "
    "side, then their average league position jumped sharply — from mid-table in 2021 to "
    "around 4th-8th in 2022-2024 — right after the club's October 2021 takeover by a new "
    "ownership group. The dataset has games, per-club per-game league position "
    "(club_games.own_position / opponent_position / own_manager_name), transfers with fees, "
    "and player valuations. You are refining THIS single investigation to understand whether "
    "the takeover actually caused the rise, and to rule out confounds."
)

ADVERSARY_SYSTEM = f"""You are a demanding senior football analyst refining ONE continuous \
investigation with an AI data-analysis tool. This is NOT a list of independent questions — \
every message builds on the tool's PREVIOUS answers, and the investigation is a TREE you \
can branch.

{ROOT_CONTEXT}

Each turn, read the tool's latest conclusion and push on its WEAKEST or most suspicious \
part. Use the full range of an analyst's moves:
- drill into a hypothesis the answer raised,
- apply a filter or EXCLUSION to isolate an effect,
- break the effect down by a derived dimension,
- CORRECT a weak or lazy DEFINITION the tool invents (tell it the better one to use),
- BRANCH: fork a new line of inquiry off an EARLIER turn instead of continuing the latest \
one. You MUST, at least once, take a single fork point and spawn TWO sibling branches that \
test COMPETING explanations (e.g. "it was the spending" vs "it was the new manager", or \
Newcastle-the-treatment vs a matched control club that was NOT taken over), then later \
COMPARE what the two branches found.
- near the end, restate / rerun the overall causal conclusion.

To branch, set branch_from_turn to the number of the earlier turn whose node you want to \
fork from (the tool will attach your question there, as a real sibling). Leave it null to \
continue the latest thread.

Rules:
- Assume the tool remembers the investigation. Refer to prior results and scope implicitly \
— you are testing whether it carries context, especially across branches.
- One concrete analytical move per turn, in a real analyst's voice (1-2 sentences). Do not \
explain your meta-strategy to the tool.
- Escalate; do not repeat a move."""


class AdversaryTurn(BaseModel):
    message: str = Field(description="The exact next message to send the analysis tool, in the analyst's voice.")
    move: Literal[
        "drill", "exclude_filter", "branch_explore", "breakdown",
        "correct_definition", "compare_branches", "rerun_restate", "other",
    ] = Field(description="The refinement move this message makes.")
    branch_from_turn: Optional[int] = Field(
        default=None,
        description="Turn number of the EARLIER turn to fork this question off (creates a real sibling branch). Null = continue the latest thread.",
    )
    targets: str = Field(description="Which specific part of a PRIOR answer (a claim, a definition, a branch) this move acts on. 'seed' for the first turn.")


def ask_adversary(client, transcript: List[dict]) -> AdversaryTurn:
    if not transcript:
        user = ("Start the investigation. Ask your first question to confirm the size and "
                "timing of Newcastle's league-position jump around the 2021 takeover.")
    else:
        lines = []
        for t in transcript:
            tag = f"[Turn {t['n']}"
            if t.get("parent_turn"):
                tag += f", branched from Turn {t['parent_turn']}"
            tag += "]"
            lines.append(f"{tag} You asked: {t['question']}")
            lines.append(f"           Tool concluded: {t['conclusion']}")
        user = ("Investigation tree so far (turn numbers; branch parents noted):\n\n"
                + "\n".join(lines) +
                "\n\nWrite your next message. If forking off an earlier turn, set "
                "branch_from_turn to that turn's number.")
    response = client._client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=ADVERSARY_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=AdversaryTurn,
    )
    client.usage.record(response.usage)
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(f"adversary returned no structured output (stop={getattr(response,'stop_reason','?')})")
    return parsed


def main():
    from exhibit.llm.anthropic_client import AnthropicLLM

    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    tool_client = AnthropicLLM(model=MODEL)
    adv_client = AnthropicLLM(model=MODEL)

    t0 = time.time()
    session = orchestrator.start_investigation(conn, paths, sorted(F.glob("*.csv")), tool_client)
    print(f"loaded {len(session.profiles)} tables in {time.time()-t0:.0f}s", flush=True)

    transcript: List[dict] = []
    results = []
    turn_conclusion: dict = {}  # turn number -> its conclusion node id (fork targets)

    for i in range(1, N_TURNS + 1):
        turn = ask_adversary(adv_client, transcript)
        q = turn.message

        parent_id = None
        parent_turn = None
        if turn.branch_from_turn and turn.branch_from_turn in turn_conclusion:
            parent_id = turn_conclusion[turn.branch_from_turn]
            parent_turn = turn.branch_from_turn

        tag = f"branch<-T{parent_turn}" if parent_turn else "thread"
        print(f"\n--- Turn {i} [{turn.move}/{tag}] targets={turn.targets!r}\n    Q: {q}", flush=True)

        c0 = tool_client.usage.calls
        cost0 = tool_client.usage.cost_usd(MODEL)
        ts = time.time()
        try:
            r = orchestrator.run_question(session, q, parent_id=parent_id)
            err = None
        except Exception as e:
            r, err = None, repr(e)
        dt = time.time() - ts

        rec = {
            "n": i, "move": turn.move, "targets": turn.targets, "question": q,
            "branch_from_turn": parent_turn, "is_branch": parent_turn is not None,
            "latency_s": round(dt, 1),
            "calls": tool_client.usage.calls - c0,
            "cost_usd": round(tool_client.usage.cost_usd(MODEL) - cost0, 4),
            "path": getattr(r, "path", "?"), "crash": err,
            "plan": [], "executed_sql": [], "tools": [], "errors": [],
            "conclusion": None, "chart": False,
            "q_parent_id": None, "q_seq": None,
        }
        if r is not None:
            rec["q_parent_id"] = r.question_node.parent_id
            rec["q_seq"] = r.question_node.seq
            if r.plan_node and isinstance(r.plan_node.payload, PlanPayload):
                for s in r.plan_node.payload.plan.steps:
                    rec["plan"].append({"intent": s.intent, "method": s.method,
                                        "sql": s.sql, "tool": s.tool, "tool_args": s.tool_args_json})
            if r.conclusion_node and isinstance(r.conclusion_node.payload, ConclusionPayload):
                rec["conclusion"] = r.conclusion_node.payload.conclusion.summary
                turn_conclusion[i] = r.conclusion_node.id
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
        transcript.append({"n": i, "question": q, "parent_turn": parent_turn,
                           "conclusion": rec["conclusion"] or err or "(no conclusion)"})
        print(f"    [{rec['path']}] {rec['latency_s']}s {rec['calls']}call ${rec['cost_usd']} "
              f"parent={str(rec['q_parent_id'])[:8]} err={bool(rec['errors']) or bool(err)}"
              f"\n    A: {(rec['conclusion'] or err or '')[:200]}", flush=True)

    out = {
        "phenomenon": "Newcastle United Oct-2021 takeover (natural experiment)",
        "total_s": round(time.time() - t0, 0),
        "tool_cost_usd": round(tool_client.usage.cost_usd(MODEL), 4),
        "adversary_cost_usd": round(adv_client.usage.cost_usd(MODEL), 4),
        "tool_usage": vars(tool_client.usage),
        "adversary_usage": vars(adv_client.usage),
        "results": results,
    }
    Path("/tmp/exhibit_branch.json").write_text(json.dumps(out, indent=2, default=str))
    n_branch = sum(1 for r in results if r["is_branch"])
    print(f"\nTOTAL {out['total_s']}s tool=${out['tool_cost_usd']} adv=${out['adversary_cost_usd']} "
          f"branches={n_branch}/{N_TURNS} → /tmp/exhibit_branch.json", flush=True)


if __name__ == "__main__":
    main()
