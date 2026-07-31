"""Time-series tools: moving averages, growth rates (MoM/YoY), and trend."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..models import DatasetProfile, ResultTable, ToolResult
from .base import Tool, ToolError, get_column, require_numeric

_GRAINS = ("day", "week", "month", "quarter", "year")


def _require_temporal(profile, name):
    col = get_column(profile, name)
    if not any(t in col.dtype.upper() for t in ("DATE", "TIMESTAMP", "TIME")):
        raise ToolError(f"date_column '{name}' is {col.dtype}, expected date/timestamp")


def _series_table(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [[_j(v) for v in r] for r in cur.fetchall()]
    return ResultTable(columns=cols, rows_preview=rows[:200], row_count=len(rows)), rows, cols


def _j(v):
    return v if v is None or isinstance(v, (str, int, float, bool)) else str(v)


class MovingAverageInput(BaseModel):
    date_column: str = Field(..., description="Date/timestamp column.")
    measure: str = Field(..., description="Numeric measure to aggregate then smooth.")
    granularity: Literal["day", "week", "month", "quarter", "year"] = "month"
    window: int = Field(3, description="Number of periods in the moving-average window.")


class MovingAverage(Tool):
    name = "moving_average"
    description = "Aggregate a measure by period and compute a trailing moving average."
    Input = MovingAverageInput

    def run(self, con, table_name, profile, inputs):
        require_numeric(profile, inputs.measure)
        _require_temporal(profile, inputs.date_column)
        w = max(1, int(inputs.window))
        d, m, t = f'"{inputs.date_column}"', f'"{inputs.measure}"', f'"{table_name}"'
        sql = (
            f"WITH agg AS (SELECT date_trunc('{inputs.granularity}', {d}) AS period, "
            f"ROUND(SUM({m}), 2) AS value FROM {t} WHERE {d} IS NOT NULL GROUP BY 1) "
            f"SELECT period, value, ROUND(AVG(value) OVER (ORDER BY period "
            f"ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW), 2) AS moving_avg "
            f"FROM agg ORDER BY period"
        )
        table, rows, _ = _series_table(con, sql)
        return ToolResult(tool=self.name, table=table,
                          metrics={"periods": len(rows), "window": w},
                          summary=f"{inputs.measure} by {inputs.granularity} with a {w}-period moving average "
                                  f"({len(rows)} periods).")


class GrowthInput(BaseModel):
    date_column: str = Field(..., description="Date/timestamp column.")
    measure: str = Field(..., description="Numeric measure to aggregate.")
    granularity: Literal["day", "week", "month", "quarter", "year"] = "month"
    periods_ago: int = Field(1, description="Compare each period to this many periods earlier (1=MoM, 12=YoY on months).")


class GrowthRates(Tool):
    name = "growth_rates"
    description = "Period-over-period growth of a measure (e.g. MoM with periods_ago=1, YoY with 12 on monthly data)."
    Input = GrowthInput

    def run(self, con, table_name, profile, inputs):
        require_numeric(profile, inputs.measure)
        _require_temporal(profile, inputs.date_column)
        lag = max(1, int(inputs.periods_ago))
        d, m, t = f'"{inputs.date_column}"', f'"{inputs.measure}"', f'"{table_name}"'
        sql = (
            f"WITH agg AS (SELECT date_trunc('{inputs.granularity}', {d}) AS period, "
            f"ROUND(SUM({m}), 2) AS value FROM {t} WHERE {d} IS NOT NULL GROUP BY 1) "
            f"SELECT period, value, LAG(value, {lag}) OVER (ORDER BY period) AS prev, "
            f"ROUND((value - LAG(value, {lag}) OVER (ORDER BY period)) "
            f"/ NULLIF(LAG(value, {lag}) OVER (ORDER BY period), 0) * 100, 2) AS pct_change "
            f"FROM agg ORDER BY period"
        )
        table, rows, _ = _series_table(con, sql)
        changes = [r[3] for r in rows if r[3] is not None]
        avg_growth = round(sum(changes) / len(changes), 2) if changes else None
        return ToolResult(tool=self.name, table=table,
                          metrics={"periods": len(rows), "avg_pct_change": avg_growth,
                                   "periods_ago": lag},
                          summary=(f"{inputs.measure} growth vs {lag} period(s) earlier; "
                                   f"average change {avg_growth}% across {len(changes)} comparisons."))


class TrendInput(BaseModel):
    date_column: str = Field(..., description="Date/timestamp column.")
    measure: str = Field(..., description="Numeric measure to trend.")
    granularity: Literal["day", "week", "month", "quarter", "year"] = "month"


class Trend(Tool):
    name = "trend"
    description = "Fit a linear trend to a measure over time (slope, R², significance) and flag the biggest change point."
    Input = TrendInput

    def run(self, con, table_name, profile, inputs):
        from scipy import stats
        require_numeric(profile, inputs.measure)
        _require_temporal(profile, inputs.date_column)
        d, m, t = f'"{inputs.date_column}"', f'"{inputs.measure}"', f'"{table_name}"'
        sql = (f"SELECT date_trunc('{inputs.granularity}', {d}) AS period, "
               f"ROUND(SUM({m}), 2) AS value FROM {t} WHERE {d} IS NOT NULL GROUP BY 1 ORDER BY 1")
        table, rows, _ = _series_table(con, sql)
        if len(rows) < 3:
            raise ToolError(f"need >=3 periods to fit a trend, got {len(rows)}")
        values = [float(r[1]) for r in rows]
        idx = list(range(len(values)))
        lr = stats.linregress(idx, values)
        # biggest period-over-period change
        deltas = [(abs(values[i] - values[i - 1]), rows[i][0], values[i] - values[i - 1])
                  for i in range(1, len(values))]
        biggest = max(deltas, key=lambda x: x[0]) if deltas else (0, None, 0)
        direction = "rising" if lr.slope > 0 else "falling" if lr.slope < 0 else "flat"
        metrics = {"slope_per_period": round(float(lr.slope), 4), "r_squared": round(float(lr.rvalue ** 2), 4),
                   "p_value": round(float(lr.pvalue), 6), "direction": direction,
                   "periods": len(values), "biggest_change_at": str(biggest[1]),
                   "biggest_change": round(float(biggest[2]), 2)}
        sig = "significant" if lr.pvalue < 0.05 else "not significant"
        return ToolResult(tool=self.name, table=table, metrics=metrics, summary=(
            f"{inputs.measure} is {direction} (slope {lr.slope:,.1f}/period, R²={lr.rvalue**2:.2f}, "
            f"trend {sig}); biggest swing {biggest[2]:+,.0f} at {biggest[1]}."))
