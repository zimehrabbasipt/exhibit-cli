import pytest

from exhibit.engine.sqlguard import SqlGuardError, guard_sql


def test_allows_plain_select_and_adds_limit():
    out = guard_sql("SELECT * FROM dataset")
    assert "limit" in out.lower()


def test_respects_existing_limit():
    out = guard_sql("SELECT * FROM dataset LIMIT 5")
    assert out.lower().count("limit") == 1
    assert "5" in out


def test_allows_cte():
    out = guard_sql(
        "WITH m AS (SELECT region, SUM(revenue) r FROM dataset GROUP BY 1) "
        "SELECT * FROM m ORDER BY r DESC"
    )
    assert "limit" in out.lower()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dataset VALUES (1)",
        "UPDATE dataset SET revenue = 0",
        "DELETE FROM dataset",
        "DROP TABLE dataset",
        "CREATE TABLE x AS SELECT 1",
        "ATTACH 'evil.db' AS e",
        "COPY dataset TO 'out.csv'",
        "INSTALL httpfs",
        "PRAGMA database_list",
        "SET memory_limit='1GB'",
    ],
)
def test_rejects_non_select(sql):
    with pytest.raises(SqlGuardError):
        guard_sql(sql)


def test_rejects_multiple_statements():
    with pytest.raises(SqlGuardError):
        guard_sql("SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_parquet('secret.parquet')",
        "SELECT * FROM glob('/**')",
    ],
)
def test_rejects_file_reading_functions(sql):
    with pytest.raises(SqlGuardError):
        guard_sql(sql)
