from pathlib import Path

from exhibit.config import AppPaths
from exhibit.data import loader
from exhibit.engine import orchestrator
from exhibit.llm.mock import MockLLM
from exhibit.models import NodeKind
from exhibit.store import db
from exhibit.store import nodes as node_store

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def test_expand_sources_folder(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n")
    (tmp_path / "b.parquet").write_bytes(b"")  # counted by suffix, content irrelevant here
    (tmp_path / "notes.txt").write_text("ignore me")
    files = loader.expand_sources([tmp_path])
    names = sorted(f.name for f in files)
    assert names == ["a.csv", "b.parquet"]  # .txt excluded


def test_expand_sources_dedupes_and_keeps_order(tmp_path):
    f = tmp_path / "sales.csv"
    f.write_text("x\n1\n")
    # explicit file first, then the folder that also contains it → no duplicate
    files = loader.expand_sources([f, tmp_path])
    assert [p.name for p in files] == ["sales.csv"]


def test_start_from_folder_loads_all(exhibit_home):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, SAMPLE_DIR, MockLLM())
    names = sorted(p.table_name for p in session.profiles)
    assert names == ["customers", "products", "sales"]  # every file in the folder
    session.close()


def test_dirty_csv_falls_back_to_full_inference(tmp_path):
    """A column that looks BIGINT in the first ~20k rows but has '-' later must
    still load (via the sample_size=-1 fallback) rather than crashing."""
    csv = tmp_path / "dirty.csv"
    lines = ["flag,val"]
    lines += [f"{i},1" for i in range(25000)]   # past DuckDB's ~20k sample window
    lines.append("25001,-")                      # value that breaks a BIGINT guess
    csv.write_text("\n".join(lines) + "\n")

    duckdb_path = tmp_path / "d.duckdb"
    loader.create_database(duckdb_path)
    schema = loader.add_table(duckdb_path, csv, "csv", "dirty")   # must not raise

    assert schema["val"].upper() in ("VARCHAR", "BIGINT")  # loaded, type coerced safely
    con = loader.open_readonly(duckdb_path)
    assert con.execute('SELECT COUNT(*) FROM "dirty"').fetchone()[0] == 25001


def test_safe_table_name_uniqueness_and_sanitize():
    existing = set()
    a = loader.safe_table_name(Path("/x/Sales Q1.csv"), existing); existing.add(a)
    b = loader.safe_table_name(Path("/y/sales_q1.parquet"), existing); existing.add(b)
    assert a == "sales_q1"
    assert b == "sales_q1_2"  # collision gets suffixed


def test_multi_table_load_profiles_and_join(exhibit_home):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(
        conn, paths,
        [SAMPLE_DIR / "sales.csv", SAMPLE_DIR / "products.csv", SAMPLE_DIR / "customers.csv"],
        MockLLM(),
    )

    names = [p.table_name for p in session.profiles]
    assert names == ["sales", "products", "customers"]         # order preserved, primary first
    assert session.investigation.table_name == "sales"

    # one dataset_profile node per table
    profile_nodes = [n for n in node_store.list_by_investigation(conn, session.investigation.id)
                     if n.kind == NodeKind.dataset_profile]
    assert len(profile_nodes) == 3

    # a real cross-table JOIN runs on the read-only connection
    joined = session.duck.execute(
        "SELECT COUNT(*) FROM sales s "
        "JOIN products p ON s.product = p.product "
        "JOIN customers c ON s.customer_id = c.customer_id"
    ).fetchone()[0]
    assert joined == 247
    session.close()


def test_add_dataset_enables_join(exhibit_home):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(conn, paths, [SAMPLE_DIR / "sales.csv"], MockLLM())
    assert [p.table_name for p in session.profiles] == ["sales"]

    profile = orchestrator.add_dataset(session, SAMPLE_DIR / "products.csv")
    assert profile.table_name == "products"
    assert [p.table_name for p in session.profiles] == ["sales", "products"]

    # the newly added table is queryable/joinable on the reopened read-only conn
    margin = session.duck.execute(
        "SELECT ROUND(SUM(s.revenue - s.quantity * p.unit_cost), 2) "
        "FROM sales s JOIN products p ON s.product = p.product"
    ).fetchone()[0]
    assert margin is not None and margin > 0
    session.close()


def test_reopened_session_loads_all_tables(exhibit_home):
    paths = AppPaths.resolve()
    conn = db.connect(paths.db_path)
    session = orchestrator.start_investigation(
        conn, paths, [SAMPLE_DIR / "sales.csv", SAMPLE_DIR / "products.csv"], MockLLM())
    inv_id = session.investigation.id
    session.close()

    from exhibit.store import investigations as inv_store
    conn2 = db.connect(paths.db_path)
    inv = inv_store.get(conn2, inv_id)
    session2 = orchestrator.open_session(conn2, paths, inv, MockLLM())
    assert [p.table_name for p in session2.profiles] == ["sales", "products"]
    session2.close()
