"""Deterministic analysis tools the LLM can invoke instead of doing math itself.

Each tool runs exact computations over the read-only DuckDB table and returns a
small, structured ``ToolResult`` — so the model interprets compact numbers
(a fitted λ, a p-value, a ranked contribution) rather than reading raw rows.
This keeps analysis correct (no LLM arithmetic), reproducible (a tool call
replays deterministically), and cheap in context.

SQL remains its own path; tools are for what SQL can't express cleanly —
distribution fits, hypothesis tests with p-values, and change decomposition.
"""

from .registry import REGISTRY, get_tool, tool_specs

__all__ = ["REGISTRY", "get_tool", "tool_specs"]
