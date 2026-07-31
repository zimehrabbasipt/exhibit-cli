import pytest

from exhibit.llm import prompts
from exhibit.llm.factory import make_client
from exhibit.llm.mock import MockLLM
from exhibit.models import (
    ColumnProfile,
    DatasetProfile,
    PlanStep,
    ResultTable,
    StepOutcome,
)


def _profile():
    return DatasetProfile(
        table_name="dataset",
        row_count=247,
        columns=[
            ColumnProfile(name="order_date", dtype="DATE", null_fraction=0.0,
                          distinct_count=156, min="2024-01-01", max="2024-08-31"),
            ColumnProfile(name="revenue", dtype="DOUBLE", null_fraction=0.0,
                          distinct_count=246, min="9.48", max="806.46"),
            ColumnProfile(name="segment", dtype="VARCHAR", null_fraction=0.0,
                          distinct_count=3, sample_values=["Consumer", "Corporate"]),
        ],
    )


# --- prompts (pure, no SDK) ------------------------------------------------ #

def test_format_profile_lists_columns_and_table():
    text = prompts.format_profile(_profile())
    assert "dataset" in text and "247" in text
    for col in ("order_date", "revenue", "segment"):
        assert col in text


def test_plan_messages_include_question():
    system, user = prompts.build_plan_messages("Why did revenue decline in June?", [_profile()])
    assert "plan" in system.lower()
    assert "Why did revenue decline in June?" in user  # question is volatile → user
    assert "sql" in system.lower()  # batch: planner emits the SQL command inline
    # catalog is stable → lives in system (cacheable), not the user message
    assert "dataset" in system and "dataset" not in user


def test_sql_messages_are_readonly_and_scoped():
    step = PlanStep(id="s1", intent="trend", description="monthly revenue")
    system, user = prompts.build_sql_messages(step, [_profile()])
    assert "read-only" in system and "SELECT" in system
    assert "dataset" in system          # catalog is cached in system
    assert "s1" in user                 # the specific step is volatile → user
    assert "INNER JOIN" in system and "anti-join" in system  # join-type guidance


def test_sql_messages_include_prior_step_for_composition():
    s1 = PlanStep(id="s1", intent="find_set", description="find ids")
    s2 = PlanStep(id="s2", intent="use_set", description="rank that set")
    prior = StepOutcome(
        step=s1, node_id="n1", sql="SELECT id FROM t WHERE flag = 1",
        table=ResultTable(columns=["id"], rows_preview=[[7]], row_count=1),
    )
    system, user = prompts.build_sql_messages(s2, [_profile()], [prior])
    assert "SELECT id FROM t WHERE flag = 1" in user   # prior SQL is available to reuse
    assert "reuse" in system.lower() or "REUSING" in system


def test_catalog_lists_multiple_tables_with_join_hint():
    p1 = _profile()
    p2 = DatasetProfile(table_name="products", row_count=4, columns=[
        ColumnProfile(name="product", dtype="VARCHAR", null_fraction=0.0),
        ColumnProfile(name="unit_cost", dtype="DOUBLE", null_fraction=0.0),
    ])
    text = prompts.format_catalog([p1, p2])
    assert "dataset" in text and "products" in text and "JOIN" in text


def test_narrate_messages_embed_result_rows():
    step = PlanStep(id="s1", intent="trend", description="monthly revenue")
    table = ResultTable(columns=["month", "total"],
                        rows_preview=[["2024-06-01", 931.09]], row_count=8)
    outcome = StepOutcome(step=step, node_id="node123", table=table)
    system, user = prompts.build_narrate_messages("why down?", [_profile()], [outcome])
    assert "931.09" in user and "month" in user


def test_plan_catalog_lists_tools():
    system, user = prompts.build_plan_messages("why did revenue fall?", [_profile()])
    assert "method='tool'" in system
    for tool in ("decompose_contribution", "volume_vs_rate", "fit_distribution"):
        assert tool in system  # tool catalog is stable → cached in system


def test_plan_warns_tools_only_read_real_columns():
    system, _ = prompts.build_plan_messages("q", [_profile()])
    # planner is told a tool's columns must be real and all on one table; cross-table
    # or computed values must go through SQL instead
    assert "real columns" in system
    assert "same" in system.lower() and "table" in system
    assert "method='sql'" in system


# --- factory --------------------------------------------------------------- #

def test_factory_mock():
    assert isinstance(make_client("mock"), MockLLM)


def test_factory_auto_without_key_is_mock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_client("auto"), MockLLM)


def test_factory_unknown_raises():
    with pytest.raises(ValueError):
        make_client("gpt")


def test_factory_anthropic_selected_when_key_present(monkeypatch):
    """auto picks the Claude adapter when SDK + key are present — but construction
    is stubbed so the test never hits the network."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import exhibit.llm.anthropic_client as ac

    class _Stub:
        name = "anthropic:stub"

        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ac, "AnthropicLLM", _Stub)
    client = make_client("auto")
    assert client.name == "anthropic:stub"
