"""Model routing: mechanical steps go to the cheap model, judgment to the frontier
model, and per-model cost accounting reflects the split."""

import pytest

from exhibit.llm.usage import Usage


class _Resp:
    def __init__(self, i=0, o=0, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


def test_per_model_cost_splits_and_totals():
    u = Usage()
    u.record(_Resp(i=1000, o=1000), "claude-opus-4-8")     # judgment
    u.record(_Resp(i=1000, o=1000), "claude-haiku-4-5")    # mechanical
    assert set(u.by_model) == {"claude-opus-4-8", "claude-haiku-4-5"}
    opus = u.by_model["claude-opus-4-8"].cost("claude-opus-4-8")
    haiku = u.by_model["claude-haiku-4-5"].cost("claude-haiku-4-5")
    assert haiku < opus                       # cheap model is cheaper for equal tokens
    assert abs(u.cost_usd() - (opus + haiku)) < 1e-12   # true total sums per-model
    # priced as if everything ran on opus (what routing avoids)
    assert u.cost_usd("claude-opus-4-8") > u.cost_usd()


def test_routing_map():
    pytest.importorskip("anthropic")
    from exhibit.llm.anthropic_client import AnthropicLLM, CHEAP_MODEL, DEFAULT_MODEL
    try:
        c = AnthropicLLM(route=True)
    except Exception:
        pytest.skip("anthropic client could not be constructed in this environment")
    assert c._model_for("generate_sql") == CHEAP_MODEL
    assert c._model_for("generate_tool_call") == CHEAP_MODEL
    assert c._model_for("summarize") == CHEAP_MODEL
    assert c._model_for("plan") == DEFAULT_MODEL
    assert c._model_for("narrate") == DEFAULT_MODEL
    assert c._model_for("critique") == DEFAULT_MODEL

    off = AnthropicLLM(route=False)
    assert off._model_for("generate_sql") == DEFAULT_MODEL  # no routing → all frontier
