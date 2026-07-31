from exhibit.llm.mock import MockLLM
from exhibit.llm.usage import Usage


class _FakeRespUsage:
    def __init__(self, i=0, o=0, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


def test_usage_records_and_totals():
    u = Usage()
    u.record(_FakeRespUsage(i=100, o=50, cr=6000, cw=7000))
    u.record(_FakeRespUsage(i=40, o=30, cr=6000, cw=0))
    assert u.calls == 2
    assert u.input_tokens == 140
    assert u.output_tokens == 80
    assert u.cache_read_tokens == 12000
    assert u.cache_write_tokens == 7000
    assert u.total_input_tokens == 140 + 12000 + 7000


def test_cost_and_cache_savings_opus():
    u = Usage(input_tokens=1_000, output_tokens=1_000,
              cache_read_tokens=1_000_000, cache_write_tokens=0)
    # Opus 4.8: $5/M in, $25/M out; cache read 0.1x
    # actual input = 1000*5 + 1e6*5*0.1 = 5,000 + 500,000 (per 1e6) = ... compute in $
    actual = u.cost_usd("claude-opus-4-8")
    uncached = u.uncached_cost_usd("claude-opus-4-8")
    assert uncached > actual                      # caching saved money
    assert u.cache_savings_usd("claude-opus-4-8") == uncached - actual
    # cache read priced at 10% of full input → big saving on the 1M cached tokens
    assert (uncached - actual) > 4.0              # ~$4.5 saved on 1M cached tokens


def test_mock_reports_zero_usage():
    m = MockLLM()
    assert m.usage.calls == 0
    assert m.pricing_model is None
    assert m.usage.cost_usd("claude-opus-4-8") == 0.0
