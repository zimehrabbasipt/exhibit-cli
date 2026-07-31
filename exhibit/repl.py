"""Interactive investigation loop.

Inside an investigation, free text is treated as an analytical question; lines
starting with ``/`` are meta commands for inspecting the persisted node log.
"""

from __future__ import annotations

from typing import List, Optional

from . import export as export_mod
from .engine import orchestrator
from .engine.orchestrator import Session
from .models import PlanPayload, SqlPayload
from pathlib import Path

from .render import console, render_history, render_node, render_profile, render_profiles
from .store import nodes as node_store

_HELP = """[bold]Commands[/bold]
  <text>          ask an analytical question (full investigation: plan, tools, follow-ups)
  /quick <text>   fast single-loop answer (fewer calls; no tools/follow-ups)
  /branch <id> <text>  fork a question off an earlier node (a real branch, not the latest thread)
  /judge [id]     adversarial review of the whole investigation (or a branch, if <id> given)
  /judge --branches <a> <b> | --descendants <id> | --unresolved   scoped graph-aware reviews
  /graph          deterministic graph checks + hypotheses with their derived status
  /metric <name> = <sql> [| desc]   define a reusable named metric (reused in later queries)
  /metrics        list defined metrics
  /schema         show all loaded table profiles
  /add <file|dir> load another CSV/Parquet (or every file in a folder) as tables
  /plan           show the most recent plan
  /history        list all investigation nodes
  /artifacts      list nodes that produced saved files
  /show <id>      render a node by (short) id
  /sql <id>       print the SQL of a sql_query node
  /rerun <id>     re-run a sql_query, or a whole plan's task list (deterministic)
  /chart <id> <type> x=<col> [y=<col>] [title=..]  chart a result table (line|bar|scatter|histogram)
  /tools          list available analysis tools
  /tool <name> k=v ...   run a tool (e.g. /tool describe column=revenue)
  /export [html]  write a report — Markdown, or a self-contained HTML viewer (no LLM)
  /cost           token usage + cost for this session (with cache savings)
  /help           show this help
  exit | quit     leave the investigation
"""


def run_repl(session: Session) -> None:
    inv = session.investigation
    console.print(
        f"[bold green]Investigation:[/bold green] {inv.name} "
        f"[dim]({inv.id[:8]})[/dim] · table [magenta]{inv.table_name}[/magenta]"
    )
    console.print("[dim]Type a question, or /help for commands.[/dim]\n")

    while True:
        try:
            line = console.input("[bold cyan]exhibit>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        if line.startswith("/"):
            _handle_command(session, line)
        else:
            _handle_question(session, line)

    console.print("[dim]Investigation saved. Reopen with[/dim] "
                  f"[bold]exhibit open {inv.id[:8]}[/bold]")


def _handle_question(
    session: Session, question: str, fast: bool = False, parent_id: str = None
) -> None:
    from .render import LiveProgress, render_run_result

    with LiveProgress() as progress:
        result = orchestrator.run_question(
            session, question, parent_id=parent_id, progress=progress, fast=fast
        )
    render_run_result(result)


def _metric(session: Session, arg: str) -> None:
    """`/metric <name> = <sql> [| description]` — define a reusable named metric so its
    definition is fed into later SQL/planning and can't silently drift."""
    if "=" not in arg:
        console.print("[red]Usage:[/red] /metric <name> = <sql expression> [| description]")
        return
    name, rest = arg.split("=", 1)
    name = name.strip()
    sql, desc = (rest.split("|", 1) + [""])[:2] if "|" in rest else (rest, "")
    node = orchestrator.define_metric(session, name, sql.strip(), desc.strip())
    console.print(f"[green]Defined metric[/green] [magenta]{name}[/magenta] "
                  f"[dim]{node.id[:8]}[/dim] — reused in subsequent queries.")


def _metrics(session: Session) -> None:
    from .models import MetricPayload
    ms = [n for n in node_store.list_by_investigation(session.conn, session.investigation.id)
          if isinstance(n.payload, MetricPayload)]
    if not ms:
        console.print("[dim]No metrics defined. Add one with /metric <name> = <sql>.[/dim]")
        return
    console.print("[bold]Defined metrics[/bold]")
    for m in ms:
        d = f"  [dim]{m.payload.description}[/dim]" if m.payload.description else ""
        console.print(f"  [magenta]{m.payload.name}[/magenta] = {m.payload.sql}{d}")


def _graph(session: Session) -> None:
    """`/graph` — run the deterministic graph checks and list hypotheses with their
    edge-derived status. No LLM."""
    from .engine import graph
    from .models import HypothesisPayload

    warnings = graph.graph_lint(session.conn, session.investigation.id)
    if warnings:
        console.print(f"[yellow]⚠ graph checks — {len(warnings)} warning(s)[/yellow]")
        for c in warnings:
            ids = ", ".join(n[:8] for n in c.node_ids)
            console.print(f"  [yellow]•[/yellow] {c.detail} [dim]({c.name}: {ids})[/dim]")
    else:
        console.print("[green]✓ graph checks: no warnings[/green]")

    hyps = [n for n in node_store.list_by_investigation(session.conn, session.investigation.id)
            if isinstance(n.payload, HypothesisPayload)]
    if hyps:
        console.print("[bold]Hypotheses[/bold]")
        for h in hyps:
            status = graph.hypothesis_status(session.conn, h.id)
            console.print(f"  [magenta]{h.id[:8]}[/magenta] [dim]{status}[/dim] — {h.payload.statement}")


def _judge(session: Session, arg: str) -> None:
    """`/judge` — adversarial LLM review over a graph-aware view. Scopes:
      /judge                     whole investigation
      /judge <id>                a branch's ancestry
      /judge --branches <a> <b>  compare two branches head-to-head
      /judge --descendants <id>  the subtree beneath a node
      /judge --unresolved        focus on still-open hypotheses
    """
    parts = arg.split() if arg else []
    try:
        with console.status("[yellow]judging…[/yellow]"):
            if parts and parts[0] == "--branches":
                if len(parts) < 3:
                    console.print("[red]Usage:[/red] /judge --branches <id-a> <id-b>")
                    return
                a, b = _resolve_node(session, parts[1]), _resolve_node(session, parts[2])
                if not a or not b:
                    return
                console.print(f"[dim]Comparing branches[/dim] {a.id[:8]} [dim]vs[/dim] {b.id[:8]}")
                critique_node = orchestrator.judge_branches(session, a.id, b.id)
            elif parts and parts[0] == "--unresolved":
                console.print("[dim]Reviewing unresolved hypotheses…[/dim]")
                critique_node = orchestrator.judge_unresolved(session)
            elif parts and parts[0] == "--descendants":
                if len(parts) < 2:
                    console.print("[red]Usage:[/red] /judge --descendants <id>")
                    return
                node = _resolve_node(session, parts[1])
                if not node:
                    return
                console.print(f"[dim]Reviewing subtree beneath[/dim] {node.id[:8]}")
                critique_node = orchestrator.judge_descendants(session, node.id)
            elif parts:
                node = _resolve_node(session, parts[0])
                if not node:
                    return
                console.print(f"[dim]Reviewing branch ending at[/dim] {node.id[:8]}")
                critique_node = orchestrator.judge_investigation(session, node.id)
            else:
                console.print("[dim]Reviewing the whole investigation…[/dim]")
                critique_node = orchestrator.judge_investigation(session)
    except Exception as e:
        console.print(f"[red]Judge failed:[/red] {e}")
        return
    render_node(critique_node)
    console.print(f"[dim]saved as[/dim] {critique_node.id[:8]}")


def _branch(session: Session, arg: str) -> None:
    """`/branch <node-id> <question>` — ask a question that forks off an earlier
    node instead of threading under the latest conclusion. The new question's
    parent is the chosen node, so the investigation graph gets a real sibling
    branch, and context is scoped to that node's ancestry (blind to other forks).
    """
    parts = arg.split(maxsplit=1)
    if len(parts) < 2:
        console.print("[red]Usage:[/red] /branch <node-id> <question>  "
                      "[dim](fork off an earlier node; see /history for ids)[/dim]")
        return
    node = _resolve_node(session, parts[0])
    if not node:
        return
    console.print(f"[dim]Branching off[/dim] {node.id[:8]} [dim]({node.kind.value})[/dim]")
    _handle_question(session, parts[1].strip(), parent_id=node.id)


def _handle_command(session: Session, line: str) -> None:
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        console.print(_HELP)
    elif cmd == "/quick":
        if arg:
            _handle_question(session, arg, fast=True)
        else:
            console.print("[red]Usage:[/red] /quick <question>")
    elif cmd == "/branch":
        _branch(session, arg)
    elif cmd == "/judge":
        _judge(session, arg)
    elif cmd == "/graph":
        _graph(session)
    elif cmd == "/metric":
        _metric(session, arg)
    elif cmd == "/metrics":
        _metrics(session)
    elif cmd == "/schema":
        render_profiles(session.profiles)
    elif cmd == "/add":
        _add_dataset(session, arg)
    elif cmd == "/history":
        render_history(node_store.list_by_investigation(session.conn, session.investigation.id))
    elif cmd == "/plan":
        _show_latest_plan(session)
    elif cmd == "/artifacts":
        _show_artifacts(session)
    elif cmd == "/show":
        _with_node(session, arg, render_node)
    elif cmd == "/sql":
        _show_sql(session, arg)
    elif cmd == "/rerun":
        _rerun(session, arg)
    elif cmd == "/chart":
        _chart(session, arg)
    elif cmd == "/tools":
        _list_tools()
    elif cmd == "/tool":
        _run_tool(session, arg)
    elif cmd == "/cost":
        from .render import render_usage

        render_usage(getattr(session.client, "usage", None),
                     getattr(session.client, "pricing_model", None))
    elif cmd == "/export":
        _export(session, html=arg.strip().lower() == "html")
    else:
        console.print(f"[red]Unknown command:[/red] {cmd}. Try /help.")


def _resolve_node(session: Session, prefix: str):
    if not prefix:
        console.print("[red]Provide a node id (see /history).[/red]")
        return None
    matches = node_store.find_by_prefix(session.conn, session.investigation.id, prefix)
    if not matches:
        console.print(f"[red]No node matches id[/red] {prefix}")
        return None
    if len(matches) > 1:
        console.print(f"[red]Ambiguous id[/red] {prefix} — matches {len(matches)} nodes.")
        return None
    return matches[0]


def _with_node(session: Session, prefix: str, fn) -> None:
    node = _resolve_node(session, prefix)
    if node:
        fn(node)


def _show_sql(session: Session, prefix: str) -> None:
    node = _resolve_node(session, prefix)
    if not node:
        return
    if not isinstance(node.payload, SqlPayload):
        console.print(f"[red]Node {node.id[:8]} is a {node.kind.value}, not a sql_query.[/red]")
        return
    console.print(node.payload.query.sql)


def _rerun(session: Session, prefix: str) -> None:
    from .models import PlanPayload, SqlPayload

    node = _resolve_node(session, prefix)
    if not node:
        return
    try:
        if isinstance(node.payload, PlanPayload):
            # re-run the whole saved task list deterministically
            result = orchestrator.rerun_plan(session, node)
            console.print(f"[green]Reran plan[/green] {node.id[:8]} "
                          f"({len(node.payload.plan.steps)} step(s))")
            for n in result.table_nodes + result.tool_nodes:
                render_node(n)
            for n in result.error_nodes:
                render_node(n)
            return
        if isinstance(node.payload, SqlPayload):
            new_node = orchestrator.rerun_sql_node(session, node)
            console.print(f"[green]Reran[/green] {node.id[:8]} → {new_node.id[:8]}")
            render_node(new_node)
            return
        console.print(f"[red]Can't rerun a {node.kind.value} node.[/red] "
                      "Use a sql_query or plan node id.")
    except Exception as e:
        console.print(f"[red]Rerun failed:[/red] {e}")


def _chart(session: Session, arg: str) -> None:
    parts = arg.split()
    if len(parts) < 2:
        console.print("[red]Usage:[/red] /chart <table-id> <line|bar|scatter|histogram> "
                      "x=<col> [y=<col>] [title=..]")
        return
    node = _resolve_node(session, parts[0])
    if not node:
        return
    from .models import TablePayload
    if not isinstance(node.payload, TablePayload):
        console.print(f"[red]Node {node.id[:8]} is a {node.kind.value}, not a result table.[/red]")
        return
    chart_type = parts[1]
    kv = {}
    for tok in parts[2:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k] = v
    if "x" not in kv:
        console.print("[red]Provide x=<column>[/red] (and y=<column> for non-histogram).")
        return
    try:
        chart_node = orchestrator.chart_table_node(
            session, node, chart_type, kv["x"], kv.get("y"), kv.get("title"))
    except Exception as e:
        console.print(f"[red]Chart failed:[/red] {e}")
        return
    console.print(f"[green]Charted[/green] {node.id[:8]} → {chart_node.id[:8]}")
    render_node(chart_node)


def _add_dataset(session: Session, arg: str) -> None:
    if not arg:
        console.print("[red]Usage:[/red] /add <file-or-folder>")
        return
    path = Path(arg).expanduser()
    if not path.exists():
        console.print(f"[red]Path not found:[/red] {path}")
        return
    from .data import loader

    try:
        files = loader.expand_sources([path])  # a folder expands to its data files
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    added = 0
    for f in files:
        try:
            profile = orchestrator.add_dataset(session, f)
        except Exception as e:
            console.print(f"[red]Could not add {f.name}:[/red] {e}")
            continue
        added += 1
        console.print(f"[green]Added table[/green] [magenta]{profile.table_name}[/magenta] "
                      f"({profile.row_count:,} rows, {len(profile.columns)} columns)")
    if added:
        console.print("[dim]You can now ask questions that join across tables. /schema to see all.[/dim]")


def _list_tools() -> None:
    from .tools import tool_specs

    for spec in tool_specs():
        console.print(f"[magenta]{spec['name']}[/magenta] — {spec['description']}")
        props = spec["input_schema"].get("properties", {})
        required = set(spec["input_schema"].get("required", []))
        params = ", ".join(
            f"{k}{'*' if k in required else ''}" for k in props
        )
        console.print(f"  [dim]params:[/dim] {params}")
    console.print("[dim](* = required) e.g. /tool describe column=revenue[/dim]")


def _run_tool(session: Session, arg: str) -> None:
    parts = arg.split()
    if not parts:
        console.print("[red]Usage:[/red] /tool <name> key=value ...  (see /tools)")
        return
    name = parts[0]
    inputs: dict = {}
    for token in parts[1:]:
        if "=" not in token:
            console.print(f"[red]Bad argument[/red] {token!r} — expected key=value.")
            return
        key, value = token.split("=", 1)
        inputs[key] = value
    node = orchestrator.run_tool(session, name, inputs)
    render_node(node)


def _show_latest_plan(session: Session) -> None:
    nodes = node_store.list_by_investigation(session.conn, session.investigation.id)
    for node in reversed(nodes):
        if isinstance(node.payload, PlanPayload):
            render_node(node)
            return
    console.print("[dim]No plans yet — ask a question first.[/dim]")


def _show_artifacts(session: Session) -> None:
    nodes = node_store.list_by_investigation(session.conn, session.investigation.id)
    with_art = [n for n in nodes if n.artifact_path]
    if not with_art:
        console.print("[dim]No saved artifacts yet.[/dim]")
        return
    for n in with_art:
        console.print(f"  {n.id[:8]}  {n.kind.value}  [dim]{n.artifact_path}[/dim]")


def _export(session: Session, html: bool = False) -> None:
    path = session.paths.export_path(session.investigation.id)
    if html:
        from . import export_html as html_mod
        content = html_mod.export_html(session.conn, session.investigation)
        path = path.with_suffix(".html")
    else:
        content = export_mod.export_markdown(session.conn, session.investigation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    console.print(f"[green]Exported[/green] → {path}")
