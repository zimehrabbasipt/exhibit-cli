"""Token usage + cost accounting for LLM calls.

Accumulated per client (i.e. per session), so a `/cost` command can report what an
investigation actually spent — and how much prompt caching saved versus paying
full price for every token.

With **model routing** a single session mixes models (a cheap model for mechanical
SQL-gen, a frontier model for judgment), so usage is tracked **per model** and cost is
the sum of each model priced at its own rate. Aggregate token fields are kept for
backward compatibility and quick totals.

Pricing is $ per million tokens (input, output). Prompt caching multipliers:
cache *writes* cost 1.25x input, cache *reads* cost 0.1x input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

# $ per 1M tokens: (input, output). See the claude-api pricing table.
_PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICING = (5.0, 25.0)
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def _pricing(model: str):
    return _PRICING.get(model, _DEFAULT_PRICING)


@dataclass
class ModelUsage:
    """Token counts for one model."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, resp_usage) -> None:
        self.calls += 1
        self.input_tokens += getattr(resp_usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(resp_usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(resp_usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(resp_usage, "cache_creation_input_tokens", 0) or 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self, model: str) -> float:
        inp, out = _pricing(model)
        return (self.input_tokens * inp
                + self.cache_write_tokens * inp * _CACHE_WRITE_MULT
                + self.cache_read_tokens * inp * _CACHE_READ_MULT
                + self.output_tokens * out) / 1_000_000

    def uncached_cost(self, model: str) -> float:
        inp, out = _pricing(model)
        return (self.total_input_tokens * inp + self.output_tokens * out) / 1_000_000


@dataclass
class Usage:
    # aggregate totals across all models (back-compat + quick reads)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    by_model: Dict[str, ModelUsage] = field(default_factory=dict)

    def record(self, resp_usage, model: str = None) -> None:
        """Fold one response's `usage` into the running totals, attributed to ``model``."""
        self.calls += 1
        self.input_tokens += getattr(resp_usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(resp_usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(resp_usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(resp_usage, "cache_creation_input_tokens", 0) or 0
        self.by_model.setdefault(model or "unknown", ModelUsage()).add(resp_usage)

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost_usd(self, model: str = None) -> float:
        """Actual cost. With no ``model``, sum each model priced at its own rate (the
        correct number under routing). With a ``model``, price all tokens at that rate
        (legacy single-model behaviour)."""
        if model is None:
            if self.by_model:
                return sum(mu.cost(m) for m, mu in self.by_model.items())
            model = "unknown"
        inp, out = _pricing(model)
        return (self.input_tokens * inp
                + self.cache_write_tokens * inp * _CACHE_WRITE_MULT
                + self.cache_read_tokens * inp * _CACHE_READ_MULT
                + self.output_tokens * out) / 1_000_000

    def uncached_cost_usd(self, model: str = None) -> float:
        """Hypothetical cost with no prompt caching (per-model when ``model`` is None)."""
        if model is None:
            if self.by_model:
                return sum(mu.uncached_cost(m) for m, mu in self.by_model.items())
            model = "unknown"
        inp, out = _pricing(model)
        return (self.total_input_tokens * inp + self.output_tokens * out) / 1_000_000

    def cache_savings_usd(self, model: str = None) -> float:
        return self.uncached_cost_usd(model) - self.cost_usd(model)
