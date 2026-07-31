"""Quantify model routing: run a few questions on the football data with routing ON,
then compare the actual per-model cost against what the same run would have cost on the
frontier model alone. (Single run — the routed run's per-model token counts let us price
the all-frontier counterfactual without paying for it twice; it's a first-order estimate
since a cheap model's token counts differ slightly from the frontier model's.)"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.llm.anthropic_client import AnthropicLLM, DEFAULT_MODEL
from exhibit.store import db

F = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()
QUESTIONS = [
    "How many games are in the dataset and what date range do they cover?",
    "Which 10 players scored the most goals, and in how many games each?",
    "Home vs away win rate and average goals per game across the dataset — how big is home advantage?",
    "For the Premier League (GB1), average attendance and home/away points-per-game gap by season.",
]


def main():
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = AnthropicLLM(route=True)
    session = orchestrator.start_investigation(conn, paths, sorted(F.glob("*.csv")), client)
    print(f"loaded {len(session.profiles)} tables; routing ON", flush=True)

    for i, q in enumerate(QUESTIONS, 1):
        orchestrator.run_question(session, q)
        print(f"  Q{i} done", flush=True)

    u = client.usage
    print("\n=== PER-MODEL USAGE ===")
    for m, mu in sorted(u.by_model.items()):
        print(f"  {m:22s} calls={mu.calls:2d}  in={mu.input_tokens:6d}  "
              f"cache_rd={mu.cache_read_tokens:7d}  out={mu.output_tokens:6d}  "
              f"cost=${mu.cost(m):.4f}")

    routed = u.cost_usd()                       # true per-model total
    all_frontier = u.cost_usd(DEFAULT_MODEL)    # same tokens, all priced as Opus
    saved = all_frontier - routed
    pct = (saved / all_frontier * 100) if all_frontier else 0.0
    print("\n=== ROUTING IMPACT ===")
    print(f"  routed total:            ${routed:.4f}")
    print(f"  all-{DEFAULT_MODEL}: ${all_frontier:.4f}  (hypothetical)")
    print(f"  saved by routing:        ${saved:.4f}  ({pct:.0f}%)")
    print(f"  calls: {u.calls}")


if __name__ == "__main__":
    main()
