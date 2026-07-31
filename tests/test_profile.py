from exhibit.data import loader
from exhibit.data import profile as profiler


def test_profile_sample(tmp_path, sample_csv):
    duckdb_path = tmp_path / "data.duckdb"
    fmt, table_name, schema = loader.load(sample_csv, duckdb_path)
    assert fmt == "csv"
    assert table_name == "dataset"

    con = loader.open_readonly(duckdb_path)
    profile = profiler.profile_dataset(con, table_name, schema)

    assert profile.row_count == 247
    names = {c.name for c in profile.columns}
    assert {"order_date", "revenue", "segment", "region"} <= names

    revenue = next(c for c in profile.columns if c.name == "revenue")
    assert any(t in revenue.dtype.upper() for t in ("DOUBLE", "DECIMAL", "FLOAT"))
    assert revenue.null_fraction == 0.0

    segment = next(c for c in profile.columns if c.name == "segment")
    assert segment.distinct_count == 3


def test_readonly_connection_blocks_writes(tmp_path, sample_csv):
    import duckdb

    duckdb_path = tmp_path / "data.duckdb"
    loader.load(sample_csv, duckdb_path)
    con = loader.open_readonly(duckdb_path)
    with __import__("pytest").raises(duckdb.Error):
        con.execute("CREATE TABLE evil AS SELECT 1")
