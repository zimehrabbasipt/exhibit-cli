"""decompose_contribution: which dimension values drove a change between periods.

Answers "which segments/regions contributed most to the decline?" by computing,
per dimension value, the change in a measure between two months and each value's
share of the *total* change. This is the demo-critical explanatory tool.
"""

from __future__ import annotations

import re

import duckdb
from pydantic import BaseModel, Field

from ..models import DatasetProfile, ResultTable, ToolResult
from .base import Tool, ToolError, get_column, require_numeric

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_MAX_ROWS = 25


class DecomposeInput(BaseModel):
    dimension: str = Field(..., description="Categorical column to break the change down by.")
    measure: str = Field(..., description="Numeric measure that changed (e.g. revenue).")
    date_column: str = Field(..., description="Date/timestamp column defining the periods.")
    period_a: str = Field(..., description="Baseline month as 'YYYY-MM' (e.g. 2024-05).")
    period_b: str = Field(..., description="Comparison month as 'YYYY-MM' (e.g. 2024-06).")


class DecomposeContributionTool(Tool):
    name = "decompose_contribution"
    description = (
        "Break the change in a measure between two months down by a categorical "
        "dimension, ranking each value's contribution to the total change."
    )
    Input = DecomposeInput

    def run(self, con, table_name, profile: DatasetProfile, inputs: DecomposeInput) -> ToolResult:
        get_column(profile, inputs.dimension)
        require_numeric(profile, inputs.measure)
        self._require_temporal(profile, inputs.date_column)
        for label, p in (("period_a", inputs.period_a), ("period_b", inputs.period_b)):
            if not _PERIOD_RE.match(p):
                raise ToolError(f"{label} must be 'YYYY-MM', got {p!r}")

        dim = f'"{inputs.dimension}"'
        meas = f'"{inputs.measure}"'
        date = f'"{inputs.date_column}"'
        t = f'"{table_name}"'
        a = f"DATE '{inputs.period_a}-01'"
        b = f"DATE '{inputs.period_b}-01'"

        rows = con.execute(
            f"SELECT {dim} AS value, "
            f"ROUND(SUM(CASE WHEN date_trunc('month', {date}) = {a} THEN {meas} ELSE 0 END), 2) AS period_a, "
            f"ROUND(SUM(CASE WHEN date_trunc('month', {date}) = {b} THEN {meas} ELSE 0 END), 2) AS period_b "
            f"FROM {t} "
            f"WHERE date_trunc('month', {date}) IN ({a}, {b}) "
            f"GROUP BY 1"
        ).fetchall()

        if not rows:
            raise ToolError(
                f"no rows found in {inputs.period_a} or {inputs.period_b} "
                f"for {inputs.date_column}"
            )

        records = []
        for value, va, vb in rows:
            va = float(va or 0.0)
            vb = float(vb or 0.0)
            records.append({"value": str(value), "period_a": va, "period_b": vb, "delta": round(vb - va, 2)})
        records.sort(key=lambda r: abs(r["delta"]), reverse=True)

        total_delta = round(sum(r["delta"] for r in records), 2)
        for r in records:
            r["contribution_pct"] = (
                round(r["delta"] / total_delta * 100, 1) if total_delta else None
            )

        top = records[0]
        direction = "increase" if total_delta >= 0 else "decrease"
        summary = (
            f"Total {inputs.measure} {direction} of {total_delta:,.2f} from "
            f"{inputs.period_a} to {inputs.period_b}. Largest contributor: "
            f"{inputs.dimension}={top['value']} "
            f"({top['delta']:+,.2f}, {top['contribution_pct']}% of the change)."
        )
        table = ResultTable(
            columns=["value", "period_a", "period_b", "delta", "contribution_pct"],
            rows_preview=[
                [r["value"], r["period_a"], r["period_b"], r["delta"], r["contribution_pct"]]
                for r in records[:_MAX_ROWS]
            ],
            row_count=len(records),
        )
        metrics = {
            "total_delta": total_delta,
            "top_contributor": top["value"],
            "top_contribution_pct": top["contribution_pct"],
        }
        return ToolResult(tool=self.name, summary=summary, metrics=metrics, table=table)

    @staticmethod
    def _require_temporal(profile: DatasetProfile, name: str) -> None:
        col = get_column(profile, name)
        if not any(t in col.dtype.upper() for t in ("DATE", "TIMESTAMP", "TIME")):
            raise ToolError(f"date_column '{name}' is {col.dtype}, expected a date/timestamp")
