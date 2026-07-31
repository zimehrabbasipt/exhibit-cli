"""Postgres snapshot import — the parts testable without a live DB: DSN detection,
credential redaction, provenance on the profile, and its rendering in the viewer.

The live path (postgres_catalog / row_counts / add_postgres_table) needs the duckdb
postgres extension + a running Postgres, so it's verified by hand, not here."""

import pytest

from exhibit.config import AppPaths
from exhibit.data import loader
from exhibit.engine import orchestrator
from exhibit.export_html import export_html
from exhibit.llm.mock import MockLLM
from exhibit.models import DatasetProfile, ProfilePayload
from exhibit.store import db
from exhibit.store import nodes as node_store


def test_is_postgres_dsn():
    assert loader.is_postgres_dsn("postgres://h/db")
    assert loader.is_postgres_dsn("postgresql://u:p@h:5432/db")
    assert not loader.is_postgres_dsn("/tmp/data.csv")
    assert not loader.is_postgres_dsn("data.parquet")


def test_redact_dsn_strips_credentials():
    r = loader.redact_dsn("postgresql://user:secret@db.internal:5432/prod?sslmode=require")
    assert "secret" not in r and "user" not in r
    assert "db.internal:5432" in r and "/prod" in r


def test_profile_truncated_flag():
    p = DatasetProfile(table_name="events", row_count=1_000_000, columns=[],
                       source="postgresql://h/db", snapshot_at="2026-07-31T14:02:00",
                       source_row_count=5_000_000)
    assert p.truncated
    p2 = DatasetProfile(table_name="orders", row_count=1200, columns=[],
                        source="postgresql://h/db", snapshot_at="x", source_row_count=1200)
    assert not p2.truncated


def test_dsn_source_requires_table_selection(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    with pytest.raises(ValueError):
        orchestrator.start_investigation(conn, paths, "postgresql://h/db", MockLLM())  # no pg_tables


def test_viewer_renders_snapshot_provenance(exhibit_home, sample_csv):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, sample_csv, MockLLM())
    # simulate a pg-snapshot profile node (what add_postgres_table would produce)
    node_store.append(
        conn, session.investigation.id,
        ProfilePayload(profile=DatasetProfile(
            table_name="orders", row_count=1_000_000, columns=[],
            source="postgresql://prod-replica:5432/app",
            snapshot_at="2026-07-31T14:02:00", source_row_count=1_200_000)),
        title="Table `orders`")
    html = export_html(conn, session.investigation)
    assert "snapshot of postgresql://prod-replica:5432/app" in html
    assert "capped from 1,200,000" in html    # truncation shown as visible provenance
    session.close()
