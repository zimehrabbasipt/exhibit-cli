"""Terminal rendering (Rich). Shared by the CLI commands and the REPL.

Kept separate from control flow so the same node can be rendered identically
whether it was just produced or fetched later via ``/show``.
"""

from __future__ import annotations

from typing import List

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .models import (
    ConclusionPayload,
    CritiquePayload,
    DatasetProfile,
    ErrorPayload,
    FollowUpPayload,
    HypothesisPayload,
    Investigation,
    MetricPayload,
    InterpretationPayload,
    Node,
    PlanPayload,
    ProfilePayload,
    QuestionPayload,
    ChartPayload,
    ResultTable,
    SqlPayload,
    SummaryPayload,
    TablePayload,
    ToolCallPayload,
    ToolResultPayload,
)

console = Console()


def short(node_id: str) -> str:
    return node_id[:8]


class LiveProgress:
    """Live spinner + step checklist shown while a question is processed.

    Implements the engine's ``Progress`` protocol. Used as a context manager: the
    display animates during blocking LLM/DB calls, then clears (transient) so the
    final rendered result is what remains on screen.
    """

    _MARK = {"pending": ("○", "dim"), "running": ("◐", "cyan"),
             "done": ("✓", "green"), "error": ("✗", "red")}

    def __init__(self) -> None:
        self._phase = "Planning the analysis…"
        self._steps: List[dict] = []
        self._live = Live(console=console, transient=True, refresh_per_second=12)

    def __enter__(self) -> "LiveProgress":
        self._live.start()
        self._refresh()
        return self

    def __exit__(self, *exc) -> None:
        self._live.stop()

    # -- Progress protocol -------------------------------------------------- #
    def phase(self, label: str) -> None:
        self._phase = label
        self._refresh()

    def start_planning(self) -> None:
        # In batch mode the plan call also writes the SQL for each step, so this
        # is where most of the wait is — say so.
        self._phase = "Planning & writing queries…"
        self._steps = []
        self._refresh()

    def plan_ready(self, plan) -> None:
        self._steps = [
            {"label": f"{s.intent} — {s.description}", "status": "pending"}
            for s in plan.steps
        ]
        n = len(plan.steps)
        self._phase = (f"Executing {n} step{'s' if n != 1 else ''}…" if n
                       else "Answering from the dataset profile…")
        self._refresh()

    def step_start(self, index: int, step) -> None:
        if 0 <= index < len(self._steps):
            self._steps[index]["status"] = "running"
            self._refresh()

    def step_done(self, index: int, step, ok: bool) -> None:
        if 0 <= index < len(self._steps):
            self._steps[index]["status"] = "done" if ok else "error"
            self._refresh()

    def start_narrating(self) -> None:
        self._phase = "Interpreting the results…"
        self._refresh()

    # -- rendering ---------------------------------------------------------- #
    def _refresh(self) -> None:
        rows = []
        for st in self._steps:
            mark, style = self._MARK[st["status"]]
            line = Text()
            line.append(f"  {mark} ", style=style)
            line.append(st["label"], style=style if st["status"] != "pending" else "dim")
            rows.append(line)
        header = Spinner("dots", text=Text(f" {self._phase}", style="bold"))
        self._live.update(Group(header, *rows))


def render_profile(profile: DatasetProfile) -> None:
    table = Table(title=f"Dataset profile · {profile.row_count:,} rows", header_style="bold")
    table.add_column("column")
    table.add_column("type")
    table.add_column("null %", justify="right")
    table.add_column("distinct", justify="right")
    table.add_column("min")
    table.add_column("max")
    for c in profile.columns:
        table.add_row(
            c.name,
            c.dtype,
            f"{c.null_fraction * 100:.1f}%",
            "" if c.distinct_count is None else f"{c.distinct_count:,}",
            (c.min or "")[:24],
            (c.max or "")[:24],
        )
    console.print(table)
    if profile.source:
        note = (f"[dim]snapshot of[/dim] {profile.source} [dim]·[/dim] {profile.snapshot_at[:16]}"
                f" [dim]·[/dim] {profile.row_count:,} rows")
        if profile.truncated:
            note += f" [yellow](capped from {profile.source_row_count:,})[/yellow]"
        console.print(note)


def render_profiles(profiles: List[DatasetProfile]) -> None:
    if len(profiles) > 1:
        console.print(f"[bold]{len(profiles)} tables loaded:[/bold] "
                      + ", ".join(f"[magenta]{p.table_name}[/magenta]" for p in profiles))
    for p in profiles:
        render_profile(p)


def render_result_table(result: ResultTable, title: str, max_rows: int = 20) -> None:
    table = Table(title=title, header_style="bold")
    for col in result.columns:
        table.add_column(str(col))
    for row in result.rows_preview[:max_rows]:
        table.add_row(*["" if v is None else str(v) for v in row])
    console.print(table)
    shown = min(len(result.rows_preview), max_rows)
    if result.row_count > shown:
        console.print(f"[dim]… {result.row_count:,} rows total[/dim]")
    if result.parquet_path:
        console.print(f"[dim]full result: {result.parquet_path}[/dim]")


def render_tool_result(payload: ToolResultPayload, node_id: str = "") -> None:
    r = payload.result
    tag = f" · {short(node_id)}" if node_id else ""
    console.print(Panel(r.summary, title=f"{r.tool}{tag}", border_style="magenta"))
    if r.metrics:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column("metric", style="dim")
        table.add_column("value")
        for k, v in r.metrics.items():
            table.add_row(k, str(v))
        console.print(table)
    if r.table is not None and r.table.rows_preview:
        render_result_table(r.table, "supporting rows")
    for c in r.caveats:
        console.print(f"[yellow]⚠ {c}[/yellow]")


def _render_chart(node, source_table: "ResultTable" = None) -> None:
    p = node.payload
    y = f", y={p.y}" if p.y else ""
    console.print(f"[bold]▮ {p.title}[/bold] [dim]({p.chart_type}, x={p.x}{y})[/dim]")
    if source_table is not None:
        from .engine import charts
        charts.terminal_preview(source_table, p.chart_type, p.x, p.y)
    if node.artifact_path:
        console.print(f"[dim]chart saved: {node.artifact_path}[/dim]")


def render_run_result(result) -> None:  # RunResult (avoid import cycle)
    q = result.question_node.payload
    if isinstance(q, QuestionPayload):
        console.print(Panel(q.question, title="Question", border_style="cyan"))
    if getattr(result, "path", "full") == "fast":
        console.print("[dim]· fast path (agentic SQL loop)[/dim]")

    if result.plan_node and isinstance(result.plan_node.payload, PlanPayload):
        plan = result.plan_node.payload.plan
        console.print(f"[bold]Plan[/bold] [dim]({short(result.plan_node.id)})[/dim]: {plan.rationale}")
        for i, step in enumerate(plan.steps, 1):
            console.print(f"  {i}. [green]{step.intent}[/green] — {step.description}")

    for node in result.table_nodes:
        if isinstance(node.payload, TablePayload):
            render_result_table(node.payload.table, f"Result · {short(node.id)}")

    for node in result.tool_nodes:
        if isinstance(node.payload, ToolResultPayload):
            render_tool_result(node.payload, node_id=node.id)

    for node in result.chart_nodes:
        if isinstance(node.payload, ChartPayload):
            src = next((t for t in result.table_nodes
                        if t.id == node.payload.source_node_id
                        and isinstance(t.payload, TablePayload)), None)
            _render_chart(node, src.payload.table if src else None)

    if result.interpretation_node and isinstance(
        result.interpretation_node.payload, InterpretationPayload
    ):
        console.print("\n[bold]Findings[/bold]")
        for f in result.interpretation_node.payload.interpretation.findings:
            console.print(f"  • {f}")

    if result.conclusion_node and isinstance(result.conclusion_node.payload, ConclusionPayload):
        c = result.conclusion_node.payload.conclusion
        console.print(
            Panel(
                c.summary,
                title=f"Conclusion ({c.confidence} confidence)",
                border_style="green",
            )
        )

    if result.follow_up_nodes:
        console.print("[bold]Suggested follow-ups[/bold]")
        for node in result.follow_up_nodes:
            if isinstance(node.payload, FollowUpPayload):
                fu = node.payload.follow_up
                console.print(f"  → [italic]{fu.question}[/italic] [dim]({fu.why})[/dim]")

    for node in result.error_nodes:
        if isinstance(node.payload, ErrorPayload):
            console.print(
                f"[red]✗ {node.payload.stage}:[/red] {node.payload.message}"
            )

    # deterministic lint (only surface warnings; passes stay quiet)
    lint = getattr(result, "lint_node", None)
    if lint is not None and isinstance(lint.payload, CritiquePayload):
        warns = [c for c in lint.payload.checks if c.status == "warn"]
        if warns:
            console.print(f"[yellow]⚠ self-check ({len(warns)}):[/yellow]")
            for c in warns:
                console.print(f"  [yellow]•[/yellow] {c.detail} [dim]({c.name})[/dim]")
            console.print("  [dim]run /judge for a full review[/dim]")


def render_history(nodes: List[Node]) -> None:
    table = Table(title="Investigation history", header_style="bold")
    table.add_column("seq", justify="right")
    table.add_column("id")
    table.add_column("kind")
    table.add_column("title")
    for n in nodes:
        style = "red" if n.status.value == "error" else ""
        table.add_row(str(n.seq), short(n.id), n.kind.value, n.title, style=style)
    console.print(table)


def render_node(node: Node) -> None:
    p = node.payload
    console.print(f"[bold]#{node.seq} {node.kind.value}[/bold] [dim]{short(node.id)}[/dim]  {node.created_at}")
    if isinstance(p, ProfilePayload):
        render_profile(p.profile)
    elif isinstance(p, QuestionPayload):
        console.print(Panel(p.question, title="Question", border_style="cyan"))
    elif isinstance(p, PlanPayload):
        console.print(f"[italic]{p.plan.rationale}[/italic]")
        for i, step in enumerate(p.plan.steps, 1):
            console.print(f"  {i}. [green]{step.intent}[/green] — {step.description}")
            if step.sql:
                console.print(f"     [magenta]sql:[/magenta] [dim]{step.sql}[/dim]")
            elif step.tool:
                console.print(f"     [magenta]tool:[/magenta] {step.tool}"
                              f"([dim]{step.tool_args_json or ''}[/dim])")
    elif isinstance(p, SqlPayload):
        console.print(Panel(p.query.sql, title="SQL", border_style="magenta"))
        if p.query.notes:
            console.print(f"[dim]{p.query.notes}[/dim]")
    elif isinstance(p, TablePayload):
        render_result_table(p.table, f"Result · {short(node.id)}")
    elif isinstance(p, ToolCallPayload):
        args = ", ".join(f"{k}={v}" for k, v in p.call.inputs.items())
        console.print(f"[magenta]tool[/magenta] {p.call.tool}([dim]{args}[/dim])")
    elif isinstance(p, ToolResultPayload):
        render_tool_result(p, node_id=node.id)
    elif isinstance(p, InterpretationPayload):
        for f in p.interpretation.findings:
            console.print(f"  • {f}")
    elif isinstance(p, ConclusionPayload):
        console.print(
            Panel(p.conclusion.summary, title=f"Conclusion ({p.conclusion.confidence})", border_style="green")
        )
    elif isinstance(p, FollowUpPayload):
        console.print(f"→ [italic]{p.follow_up.question}[/italic] [dim]({p.follow_up.why})[/dim]")
    elif isinstance(p, ChartPayload):
        _render_chart(node)
    elif isinstance(p, SummaryPayload):
        console.print(Panel(p.text, title="Investigation summary", border_style="dim"))
    elif isinstance(p, HypothesisPayload):
        console.print(f"[bold magenta]Hypothesis[/bold magenta] ([dim]{p.origin}[/dim]): {p.statement}")
    elif isinstance(p, MetricPayload):
        console.print(f"[bold magenta]Metric[/bold magenta] {p.name} = {p.sql}"
                      + (f"  [dim]{p.description}[/dim]" if p.description else ""))
    elif isinstance(p, CritiquePayload):
        if p.mode == "review" and p.review is not None:
            render_review(p.review)
        else:
            _render_lint(p)
    elif isinstance(p, ErrorPayload):
        console.print(f"[red]Error in {p.stage}:[/red] {p.message}")


def _render_lint(p: CritiquePayload) -> None:
    warns = [c for c in p.checks if c.status == "warn"]
    if not warns:
        console.print(f"[green]✓ self-check: {len(p.checks)} checks passed[/green]")
        return
    console.print(f"[yellow]⚠ self-check — {len(warns)} warning(s)[/yellow]")
    for c in warns:
        console.print(f"  [yellow]•[/yellow] {c.detail} [dim]({c.name})[/dim]")


def render_review(r) -> None:  # JudgeReview
    """Render the on-demand LLM judge's structured critique."""
    console.print(Panel(r.overall, title="Investigation review", border_style="yellow"))
    if getattr(r, "claims", None):
        console.print("[bold]Claims[/bold] [dim](conclusion decomposed)[/dim]")
        _mark = {"supported": "[green]✓[/green]", "weak": "[yellow]~[/yellow]",
                 "unsupported": "[red]✗[/red]"}
        for c in r.claims:
            console.print(f"  {_mark.get(c.verdict, '?')} [{c.claim_type}] {c.text}\n"
                          f"    [dim]{c.why}[/dim]")
    if r.weak_conclusions:
        console.print("[bold]Weakly supported[/bold]")
        for w in r.weak_conclusions:
            console.print(f"  [yellow]•[/yellow] {w.claim}\n    [dim]{w.why}[/dim]")
    if r.untested_alternatives:
        console.print("[bold]Untested alternatives[/bold]")
        for a in r.untested_alternatives:
            console.print(f"  [cyan]•[/cyan] {a.hypothesis}\n    [dim]test: {a.how_to_test}[/dim]")
    if r.assumptions:
        console.print("[bold]Assumptions[/bold]")
        for a in r.assumptions:
            console.print(f"  [dim]•[/dim] {a}")
    if r.missing_evidence:
        console.print("[bold]Missing evidence[/bold]")
        for m in r.missing_evidence:
            console.print(f"  [dim]•[/dim] {m}")
    if r.simpler_explanation:
        console.print(f"[bold]Simpler explanation:[/bold] {r.simpler_explanation}")
    if r.confidence_assessment:
        console.print(f"[bold]Confidence:[/bold] [dim]{r.confidence_assessment}[/dim]")
    console.print(Panel(f"[italic]{r.next_query.question}[/italic]\n[dim]{r.next_query.why}[/dim]",
                        title="Highest-value next query", border_style="cyan"))


def render_usage(usage, model) -> None:
    """Token + cost summary for the session: per-model (model routing) breakdown,
    the true total, and prompt-cache savings."""
    if usage is None or usage.calls == 0:
        console.print("[dim]No LLM usage recorded this session "
                      "(mock backend, or no questions asked yet).[/dim]")
        return

    by_model = getattr(usage, "by_model", None) or {}
    routed = len(by_model) > 1

    table = Table(title="LLM usage this session", header_style="bold")
    table.add_column("model")
    table.add_column("calls", justify="right")
    table.add_column("in (uncached)", justify="right")
    table.add_column("cache rd", justify="right")
    table.add_column("cache wr", justify="right")
    table.add_column("out", justify="right")
    table.add_column("cost $", justify="right")
    if by_model:
        for m, mu in sorted(by_model.items()):
            table.add_row(m, f"{mu.calls:,}", f"{mu.input_tokens:,}",
                          f"{mu.cache_read_tokens:,}", f"{mu.cache_write_tokens:,}",
                          f"{mu.output_tokens:,}", f"{mu.cost(m):.4f}")
    else:
        table.add_row(model or "unknown", f"{usage.calls:,}", f"{usage.input_tokens:,}",
                      f"{usage.cache_read_tokens:,}", f"{usage.cache_write_tokens:,}",
                      f"{usage.output_tokens:,}", f"{usage.cost_usd(model):.4f}")
    console.print(table)

    actual = usage.cost_usd()          # per-model true total
    uncached = usage.uncached_cost_usd()
    saved = uncached - actual
    pct = (saved / uncached * 100) if uncached else 0.0
    console.print(f"[bold]Total cost:[/bold] ${actual:.4f}  "
                  f"[dim](no prompt caching: ${uncached:.4f} — saved ${saved:.4f}, {pct:.0f}%)[/dim]")
    if routed:
        # what the whole session would have cost on the frontier model alone
        allsmart = sum(mu.cost(model) for mu in by_model.values()) if model else None
        if allsmart:
            rsaved = allsmart - actual
            rpct = (rsaved / allsmart * 100) if allsmart else 0.0
            console.print(f"[bold]Routing saved:[/bold] ${rsaved:.4f} "
                          f"[dim](vs ${allsmart:.4f} if every call used {model}; {rpct:.0f}%)[/dim]")


def render_investigations(items: List[Investigation]) -> None:
    if not items:
        console.print("[dim]No investigations yet. Run `exhibit start <file>`.[/dim]")
        return
    table = Table(title="Investigations", header_style="bold")
    table.add_column("id")
    table.add_column("name")
    table.add_column("data")
    table.add_column("created")
    for inv in items:
        table.add_row(short(inv.id), inv.name, inv.data_format, inv.created_at)
    console.print(table)
