"""Self-contained HTML export — deterministic, no LLM calls, embeds charts."""

from exhibit.config import AppPaths
from exhibit.engine import orchestrator
from exhibit.export_html import export_html
from exhibit.llm.mock import MockLLM
from exhibit.store import db


def _investigation(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    orchestrator.run_question(session, "monthly revenue trend")  # yields a chart + evidence
    return conn, session


def test_html_export_is_self_contained_and_structured(exhibit_home, sample_csv):
    conn, session = _investigation(exhibit_home, sample_csv)
    html = export_html(conn, session.investigation)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "var(--surface" in html   # ships its own CSS
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "Evidence chain" in html
    assert 'class="conf"' in html                            # confidence pill
    assert 'href="#n-' in html                               # evidence chips are anchors
    session.close()


def test_html_export_embeds_chart_inline(exhibit_home, sample_csv):
    conn, session = _investigation(exhibit_home, sample_csv)
    html = export_html(conn, session.investigation)
    # the auto-generated chart PNG is inlined as base64 — no external file reference
    assert "data:image/png;base64," in html
    session.close()


def test_html_export_makes_no_llm_calls(exhibit_home, sample_csv):
    conn, session = _investigation(exhibit_home, sample_csv)
    calls_before = session.client.usage.calls   # MockLLM: always 0, but assert export adds none
    export_html(conn, session.investigation)
    assert session.client.usage.calls == calls_before
    session.close()


def test_html_export_is_dark_interactive_collapsible(exhibit_home, sample_csv):
    conn, session = _investigation(exhibit_home, sample_csv)
    r1 = [n for n in _nodes(conn, session) if n.kind.value == "conclusion"][0]
    orchestrator.run_question(session, "revenue by region")             # main-thread follow-up
    orchestrator.run_question(session, "revenue by segment", parent_id=r1.id)  # a real fork off r1
    html = export_html(conn, session.investigation)
    assert "--surface-0:#0f0f12" in html                 # dark theme
    assert "function showBranch" in html                 # branch switching JS
    assert html.count('class="chip" data-b=') >= 2       # main + branch chips
    assert 'details class="card" open' in html           # collapsible question cards
    session.close()


def _nodes(conn, session):
    from exhibit.store import nodes as ns
    return ns.list_by_investigation(conn, session.investigation.id)


def test_html_export_surfaces_caveats(exhibit_home, sample_csv):
    conn, session = _investigation(exhibit_home, sample_csv)
    # define the same metric two ways -> deterministic metric_drift caveat, no LLM
    orchestrator.define_metric(session, "m", "SUM(a)")
    orchestrator.define_metric(session, "m", "SUM(b)")
    html = export_html(conn, session.investigation)
    assert "Judge caveats" in html and "metric_drift" in html
    session.close()
