"""describe: exact summary statistics for a numeric column (pure SQL)."""

from __future__ import annotations

import duckdb
from pydantic import BaseModel, Field

from ..models import DatasetProfile, ToolResult
from .base import Tool, require_numeric


class DescribeInput(BaseModel):
    column: str = Field(..., description="Numeric column to summarize.")


class DescribeTool(Tool):
    name = "describe"
    description = (
        "Exact summary statistics (count, mean, std dev, min, quartiles, max, "
        "null fraction) for a numeric column."
    )
    Input = DescribeInput

    def run(self, con, table_name, profile: DatasetProfile, inputs: DescribeInput) -> ToolResult:
        require_numeric(profile, inputs.column)
        c = f'"{inputs.column}"'
        t = f'"{table_name}"'
        row = con.execute(
            f"SELECT count(*), count({c}), avg({c}), stddev_samp({c}), min({c}), "
            f"quantile_cont({c}, 0.25), median({c}), quantile_cont({c}, 0.75), max({c}) "
            f"FROM {t}"
        ).fetchone()
        n, non_null, mean, std, mn, q1, med, q3, mx = row
        null_fraction = (n - non_null) / n if n else 0.0
        metrics = {
            "count": int(non_null),
            "mean": _f(mean),
            "stddev": _f(std),
            "min": _f(mn),
            "p25": _f(q1),
            "median": _f(med),
            "p75": _f(q3),
            "max": _f(mx),
            "null_fraction": round(null_fraction, 4),
        }
        summary = (
            f"{inputs.column}: mean={_fmt(mean)}, std={_fmt(std)}, "
            f"range=[{_fmt(mn)}, {_fmt(mx)}], median={_fmt(med)} (n={int(non_null)})."
        )
        caveats = []
        if non_null == 0:
            caveats.append("column has no non-null values")
        return ToolResult(tool=self.name, summary=summary, metrics=metrics, caveats=caveats)


def _f(v):
    return None if v is None else float(v)


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):,.2f}"
