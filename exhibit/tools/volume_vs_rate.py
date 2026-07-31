"""volume_vs_rate: split a measure's change into volume vs. rate effects.

Answers "was the decline caused by fewer orders or lower average order value?"
Decomposes Δtotal between two months into:
    volume_effect = (n_b - n_a) * avg_a          # more/fewer records
    rate_effect   = (avg_b - avg_a) * n_a        # higher/lower per-record value
    interaction   = (n_b - n_a) * (avg_b - avg_a)
which sum exactly to total_b - total_a.
"""

from __future__ import annotations

import re

import duckdb
from pydantic import BaseModel, Field

from ..models import DatasetProfile, ToolResult
from .base import Tool, ToolError, get_column, require_numeric

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


class VolumeVsRateInput(BaseModel):
    measure: str = Field(..., description="Numeric measure (e.g. revenue).")
    date_column: str = Field(..., description="Date/timestamp column defining the periods.")
    period_a: str = Field(..., description="Baseline month 'YYYY-MM'.")
    period_b: str = Field(..., description="Comparison month 'YYYY-MM'.")


class VolumeVsRateTool(Tool):
    name = "volume_vs_rate"
    description = (
        "Decompose the change in a measure between two months into a volume "
        "effect (record count) and a rate effect (average value per record)."
    )
    Input = VolumeVsRateInput

    def run(self, con, table_name, profile: DatasetProfile, inputs: VolumeVsRateInput) -> ToolResult:
        require_numeric(profile, inputs.measure)
        col = get_column(profile, inputs.date_column)
        if not any(t in col.dtype.upper() for t in ("DATE", "TIMESTAMP", "TIME")):
            raise ToolError(f"date_column '{inputs.date_column}' is {col.dtype}, expected date/timestamp")
        for label, p in (("period_a", inputs.period_a), ("period_b", inputs.period_b)):
            if not _PERIOD_RE.match(p):
                raise ToolError(f"{label} must be 'YYYY-MM', got {p!r}")

        na, ta = self._period_stats(con, table_name, inputs, inputs.period_a)
        nb, tb = self._period_stats(con, table_name, inputs, inputs.period_b)
        if na == 0 or nb == 0:
            raise ToolError(
                f"one period has no rows (n_{inputs.period_a}={na}, n_{inputs.period_b}={nb})"
            )

        avg_a = ta / na
        avg_b = tb / nb
        volume = (nb - na) * avg_a
        rate = (avg_b - avg_a) * na
        interaction = (nb - na) * (avg_b - avg_a)
        delta = tb - ta

        driver = "volume" if abs(volume) >= abs(rate) else "rate"
        metrics = {
            "delta_total": round(delta, 2),
            "orders_a": na, "orders_b": nb,
            "avg_a": round(avg_a, 2), "avg_b": round(avg_b, 2),
            "volume_effect": round(volume, 2),
            "rate_effect": round(rate, 2),
            "interaction": round(interaction, 2),
            "primary_driver": driver,
        }
        human = "fewer orders" if nb < na else "more orders"
        rate_dir = "lower avg order value" if avg_b < avg_a else "higher avg order value"
        summary = (
            f"{inputs.measure} changed by {delta:,.2f} ({inputs.period_a}→{inputs.period_b}). "
            f"Volume effect {volume:,.2f} ({human}: {na}→{nb}); "
            f"rate effect {rate:,.2f} ({rate_dir}: {avg_a:,.2f}→{avg_b:,.2f}). "
            f"Primarily driven by {driver}."
        )
        return ToolResult(tool=self.name, summary=summary, metrics=metrics)

    @staticmethod
    def _period_stats(con, table_name, inputs: VolumeVsRateInput, period: str):
        date = f'"{inputs.date_column}"'
        meas = f'"{inputs.measure}"'
        t = f'"{table_name}"'
        n, total = con.execute(
            f"SELECT count(*), COALESCE(SUM({meas}), 0) FROM {t} "
            f"WHERE date_trunc('month', {date}) = DATE '{period}-01'"
        ).fetchone()
        return int(n), float(total)
