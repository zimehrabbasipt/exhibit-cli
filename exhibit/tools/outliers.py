"""outliers: flag anomalous values in a numeric column via IQR or z-score."""

from __future__ import annotations

from typing import Literal

import duckdb
from pydantic import BaseModel, Field

from ..models import DatasetProfile, ResultTable, ToolResult
from .base import Tool, ToolError, require_numeric

_MAX_ROWS = 25


class OutliersInput(BaseModel):
    column: str = Field(..., description="Numeric column to scan for outliers.")
    method: Literal["iqr", "zscore"] = Field("iqr", description="Detection method.")
    threshold: float = Field(
        1.5, description="IQR multiplier (default 1.5) or z-score cutoff (e.g. 3.0)."
    )


class OutliersTool(Tool):
    name = "outliers"
    description = (
        "Detect outliers in a numeric column using the IQR rule or a z-score "
        "cutoff. Returns the bounds, the count/fraction flagged, and sample rows."
    )
    Input = OutliersInput

    def run(self, con, table_name, profile: DatasetProfile, inputs: OutliersInput) -> ToolResult:
        require_numeric(profile, inputs.column)
        c = f'"{inputs.column}"'
        t = f'"{table_name}"'

        if inputs.method == "iqr":
            q1, q3 = con.execute(
                f"SELECT quantile_cont({c}, 0.25), quantile_cont({c}, 0.75) FROM {t}"
            ).fetchone()
            if q1 is None:
                raise ToolError(f"column '{inputs.column}' has no values")
            iqr = q3 - q1
            lower = q1 - inputs.threshold * iqr
            upper = q3 + inputs.threshold * iqr
        else:  # zscore
            mean, std = con.execute(
                f"SELECT avg({c}), stddev_samp({c}) FROM {t}"
            ).fetchone()
            if std in (None, 0):
                raise ToolError("cannot compute z-scores: zero or undefined std dev")
            lower = mean - inputs.threshold * std
            upper = mean + inputs.threshold * std

        total = con.execute(f"SELECT count({c}) FROM {t}").fetchone()[0]
        n_out = con.execute(
            f"SELECT count(*) FROM {t} WHERE {c} < ? OR {c} > ?", [lower, upper]
        ).fetchone()[0]
        sample = con.execute(
            f"SELECT * FROM {t} WHERE {c} < ? OR {c} > ? LIMIT {_MAX_ROWS}",
            [lower, upper],
        )
        columns = [d[0] for d in sample.description]
        rows = [[_j(v) for v in r] for r in sample.fetchall()]

        metrics = {
            "method": inputs.method,
            "lower_bound": round(float(lower), 4),
            "upper_bound": round(float(upper), 4),
            "outlier_count": int(n_out),
            "outlier_fraction": round(n_out / total, 4) if total else 0.0,
        }
        summary = (
            f"{n_out} of {total} values ({metrics['outlier_fraction'] * 100:.1f}%) "
            f"fall outside [{lower:,.2f}, {upper:,.2f}] by {inputs.method}."
        )
        table = ResultTable(columns=columns, rows_preview=rows, row_count=int(n_out))
        return ToolResult(tool=self.name, summary=summary, metrics=metrics, table=table)


def _j(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
