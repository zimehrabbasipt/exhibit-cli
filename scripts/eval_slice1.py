"""Quick Slice-1 eval on real football data: does the typed edge DAG + graph checks
behave on a genuine branched investigation?

Flow: root question (Newcastle position jump) → TWO divergent branches forked off the
root conclusion (manager explanation vs spending explanation) → LLM /judge (materializes
untested alternatives as hypothesis nodes) → deterministic graph_lint. Then dump the graph:
branch structure, edges (with provenance + status), hypotheses with edge-derived status,
and which checks fired. Writes /tmp/exhibit_slice1.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("EXHIBIT_HOME", tempfile.mkdtemp() + "/exhibit")

from exhibit.config import AppPaths
from exhibit.engine import graph, orchestrator
from exhibit.llm.anthropic_client import AnthropicLLM
from exhibit.models import ConclusionPayload, CritiquePayload, HypothesisPayload
from exhibit.store import db
from exhibit.store import edges as edge_store
from exhibit.store import nodes as node_store

F = Path(os.environ.get("FOOTBALL_DIR", "~/Downloads/football")).expanduser()


def main():
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = AnthropicLLM()
    session = orchestrator.start_investigation(conn, paths, sorted(F.glob("*.csv")), client)
    inv = session.investigation.id
    print(f"loaded {len(session.profiles)} tables", flush=True)

    root = orchestrator.run_question(
        session,
        "For Newcastle United in the Premier League (GB1), show average league "
        "position (club_games.own_position) per season 2015-2024 and the size of the "
        "jump around the October 2021 takeover.")
    print(f"root: {root.conclusion_node.id[:8]}", flush=True)

    # two divergent branches off the SAME root conclusion (competing explanations)
    bra = orchestrator.run_question(
        session,
        "Was the improvement driven by the managerial change? Split season 2021 by "
        "own_manager_name and compare average own_position under each manager.",
        parent_id=root.conclusion_node.id)
    print(f"branch A (manager): {bra.conclusion_node.id[:8]}", flush=True)

    brb = orchestrator.run_question(
        session,
        "Was the improvement driven by squad investment? Compare Newcastle's season-start "
        "squad market value (player_valuations) against average own_position 2021-2024.",
        parent_id=root.conclusion_node.id)
    print(f"branch B (spending): {brb.conclusion_node.id[:8]}", flush=True)

    # on-demand judge over the whole graph (materializes untested alternatives)
    review = orchestrator.judge_investigation(session)
    print(f"judge review: {review.id[:8]}", flush=True)

    # deterministic graph checks
    checks = graph.graph_lint(conn, inv)

    # ---- dump graph state ----
    nodes = node_store.list_by_investigation(conn, inv)
    by_id = {n.id: n for n in nodes}
    edges = edge_store.list_by_investigation(conn, inv)
    concls = [n for n in nodes if isinstance(n.payload, ConclusionPayload)]
    hyps = [n for n in nodes if isinstance(n.payload, HypothesisPayload)]

    out = {
        "n_nodes": len(nodes), "n_edges": len(edges),
        "branch_structure": [
            {"turn": lbl, "conclusion": n.id[:8], "parent": (n.parent_id or "")[:8]}
            for lbl, n in [("root", root.conclusion_node),
                           ("A/manager", bra.conclusion_node),
                           ("B/spending", brb.conclusion_node)]
        ],
        "edges": [
            {"rel": e.relationship.value, "by": e.created_by.value, "status": e.status.value,
             "src": e.source_id[:8], "dst": e.target_id[:8],
             "src_kind": by_id[e.source_id].kind.value if e.source_id in by_id else "?",
             "dst_kind": by_id[e.target_id].kind.value if e.target_id in by_id else "?"}
            for e in edges
        ],
        "hypotheses": [
            {"id": h.id[:8], "origin": h.payload.origin,
             "status": graph.hypothesis_status(conn, h.id),
             "statement": h.payload.statement}
            for h in hyps
        ],
        "graph_checks": [
            {"name": c.name, "status": c.status, "detail": c.detail,
             "node_ids": [x[:8] for x in c.node_ids]}
            for c in checks
        ],
        "conclusions": [{"id": c.id[:8], "summary": c.payload.conclusion.summary[:200]}
                        for c in concls],
    }
    Path("/tmp/exhibit_slice1.json").write_text(json.dumps(out, indent=2))

    # ---- console summary ----
    print("\n=== BRANCH STRUCTURE ===")
    for b in out["branch_structure"]:
        print(f"  {b['turn']:12s} concl={b['conclusion']} parent={b['parent']}")
    print("\n=== EDGES ===")
    from collections import Counter
    ec = Counter((e['rel'], e['by'], e['status']) for e in out['edges'])
    for (rel, by, st), n in sorted(ec.items()):
        print(f"  {n:3d}  {rel:14s} by={by:8s} {st}")
    print("\n=== HYPOTHESES (status derived from edges) ===")
    for h in out["hypotheses"]:
        print(f"  {h['id']} [{h['status']}] ({h['origin']}) {h['statement'][:90]}")
    print("\n=== GRAPH CHECKS ===")
    if not checks:
        print("  (none fired)")
    for c in out["graph_checks"]:
        print(f"  ⚠ {c['name']}: {c['detail']} -> {c['node_ids']}")
    print("\n→ /tmp/exhibit_slice1.json")


if __name__ == "__main__":
    main()
