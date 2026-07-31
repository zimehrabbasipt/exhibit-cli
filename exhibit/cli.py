"""Typer CLI entry point.

Top-level commands manage investigations; the interactive work happens in the
REPL (``exhibit/repl.py``) entered by ``start`` and ``open``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import typer

from .config import AppPaths
from .engine import orchestrator
from .llm.base import LLMClient
from .llm.factory import make_client
from .render import console, render_investigations
from .repl import run_repl
from .store import db
from .store import investigations as inv_store

_LLM_HELP = "LLM backend: auto | mock | anthropic (or set ANACLI_LLM)."

app = typer.Typer(
    add_completion=False,
    help="AI-powered data analysis CLI — analysis as a persistent investigation.",
)


def _client(preference: Optional[str] = None) -> LLMClient:
    pref = preference or os.environ.get("ANACLI_LLM") or "auto"
    return make_client(pref)


def _client_or_exit(conn, preference: Optional[str]) -> LLMClient:
    """Build the LLM client, exiting cleanly if the backend can't initialize
    (missing SDK, missing credentials, unknown preference)."""
    try:
        return _client(preference)
    except Exception as e:
        console.print(f"[red]Could not initialize LLM backend:[/red] {e}")
        conn.close()
        raise typer.Exit(code=1)


@app.command()
def start(
    files: List[str] = typer.Argument(..., help="CSV/Parquet files or folder — OR a postgres://… DSN."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Investigation name."),
    llm: Optional[str] = typer.Option(None, "--llm", "-l", help=_LLM_HELP),
    tables: Optional[str] = typer.Option(None, "--tables", help="Comma-separated tables to snapshot from a Postgres DSN (e.g. orders,public.customers)."),
    max_rows: int = typer.Option(1_000_000, "--max-rows", help="Per-table row cap on Postgres import."),
) -> None:
    """Load one or more datasets, profile them, and open an interactive investigation.
    Sources are local CSV/Parquet files/folders, or a Postgres DSN (selected tables are
    snapshotted read-only into a local copy)."""
    from .data import loader

    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    client = _client_or_exit(conn, llm)
    dsn = next((f for f in files if loader.is_postgres_dsn(f)), None)
    try:
        if dsn:
            session = _start_postgres(conn, paths, client, dsn, tables, max_rows, name)
        else:
            session = orchestrator.start_investigation(
                conn, paths, [Path(f) for f in files], client, name=name)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Could not start investigation:[/red] {e}")
        conn.close()
        raise typer.Exit(code=1)
    render_and_repl(session)


def _start_postgres(conn, paths, client, dsn, tables, max_rows, name):
    """Table-selection-required Postgres snapshot: enumerate the catalog, require an
    explicit --tables choice, show row counts for consent, warn loudly on capping."""
    from .data import loader

    try:
        catalog = loader.postgres_catalog(dsn)
    except Exception as e:
        console.print(f"[red]Could not read the Postgres catalog:[/red] {e}\n"
                      "[dim]Needs the duckdb postgres extension + network; use a read-only role.[/dim]")
        raise typer.Exit(code=1)

    if not tables:
        console.print(f"[bold]{len(catalog)} table(s) in[/bold] {loader.redact_dsn(dsn)}:")
        for c in catalog:
            console.print(f"  {c['schema']}.{c['table']}")
        console.print("\n[yellow]Select tables to snapshot[/yellow] — re-run with "
                      "[bold]--tables a,b,…[/bold] (never defaults to importing everything).")
        raise typer.Exit(code=0)

    avail = {(c["schema"], c["table"]) for c in catalog}
    selected: List = []
    for w in [t.strip() for t in tables.split(",") if t.strip()]:
        schema, tbl = w.split(".", 1) if "." in w else ("public", w)
        if (schema, tbl) not in avail:
            console.print(f"[red]No such table:[/red] {w}")
            raise typer.Exit(code=1)
        selected.append((schema, tbl))

    # consent: show per-table counts + total, warn loudly on cap
    counts = loader.postgres_row_counts(dsn, selected)
    total, any_capped = 0, False
    console.print("[bold]Snapshot size:[/bold]")
    for c in counts:
        rows = c["rows"] or 0
        capped = bool(max_rows) and rows > max_rows
        any_capped = any_capped or capped
        total += min(rows, max_rows) if max_rows else rows
        note = f"  [yellow]→ capped at {max_rows:,}[/yellow]" if capped else ""
        console.print(f"  {c['schema']}.{c['table']}: {rows:,} rows{note}")
    if any_capped:
        console.print(f"[bold yellow]⚠ Some tables exceed the {max_rows:,}-row cap and will be "
                      f"TRUNCATED — raise it with --max-rows if you need the full table.[/bold yellow]")
    if not typer.confirm(f"Copy ~{total:,} rows into a local read-only snapshot?", default=True):
        raise typer.Exit(code=0)

    return orchestrator.start_investigation(
        conn, paths, dsn, client, name=name, pg_tables=selected, max_rows=max_rows)


@app.command(name="open")
def open_investigation(
    id_or_name: str = typer.Argument(..., help="Investigation id (or name)."),
    llm: Optional[str] = typer.Option(None, "--llm", "-l", help=_LLM_HELP),
) -> None:
    """Reopen a saved investigation and continue where you left off."""
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    investigation = inv_store.resolve(conn, id_or_name)
    if investigation is None:
        # allow short-id prefix match
        investigation = _resolve_prefix(conn, id_or_name)
    if investigation is None:
        console.print(f"[red]No investigation found for[/red] {id_or_name}")
        conn.close()
        raise typer.Exit(code=1)
    client = _client_or_exit(conn, llm)
    session = orchestrator.open_session(conn, paths, investigation, client)
    render_and_repl(session)


@app.command(name="list")
def list_investigations() -> None:
    """List saved investigations."""
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    try:
        render_investigations(inv_store.list_all(conn))
    finally:
        conn.close()


@app.command()
def export(
    id_or_name: str = typer.Argument(..., help="Investigation id (or name)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output path."),
    html: bool = typer.Option(False, "--html", help="Self-contained HTML viewer instead of Markdown (no LLM; embeds charts)."),
) -> None:
    """Write a report for an investigation — Markdown by default, or a self-contained
    HTML viewer with --html. Both are deterministic (no LLM calls)."""
    from . import export as export_mod
    from . import export_html as html_mod

    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    try:
        investigation = inv_store.resolve(conn, id_or_name) or _resolve_prefix(conn, id_or_name)
        if investigation is None:
            console.print(f"[red]No investigation found for[/red] {id_or_name}")
            raise typer.Exit(code=1)
        if html:
            content = html_mod.export_html(conn, investigation)
            target = out or paths.export_path(investigation.id).with_suffix(".html")
        else:
            content = export_mod.export_markdown(conn, investigation)
            target = out or paths.export_path(investigation.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        console.print(f"[green]Exported[/green] → {target}")
    finally:
        conn.close()


def _resolve_prefix(conn, prefix: str):
    for inv in inv_store.list_all(conn):
        if inv.id.startswith(prefix):
            return inv
    return None


def render_and_repl(session: orchestrator.Session) -> None:
    from .render import render_profiles

    console.print(f"[dim]LLM backend: {session.client.name}[/dim]")
    render_profiles(session.profiles)
    try:
        run_repl(session)
    finally:
        session.close()


if __name__ == "__main__":
    app()
