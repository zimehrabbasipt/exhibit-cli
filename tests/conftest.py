from pathlib import Path

import pytest


SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample_data" / "sales.csv"


@pytest.fixture
def exhibit_home(tmp_path, monkeypatch):
    """Point Exhibit's home directory at a temp dir for isolated state."""
    home = tmp_path / "exhibit_home"
    monkeypatch.setenv("EXHIBIT_HOME", str(home))
    return home


@pytest.fixture
def sample_csv():
    assert SAMPLE_CSV.exists(), "run scripts/make_sample.py first"
    return SAMPLE_CSV
