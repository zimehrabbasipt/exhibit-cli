"""fit_distribution: fit a Poisson or Gaussian to a column with goodness-of-fit.

This is what SQL can't do: fit distribution parameters and return a statistical
test of whether the data actually follows that distribution.

- gaussian: μ, σ + D'Agostino–Pearson normality test (p < 0.05 ⇒ not normal).
- poisson:  λ = mean + a dispersion test. Under Poisson, variance ≈ mean, so
            (n-1)·var/mean ~ χ²(n-1); large dispersion ⇒ over-dispersed, not Poisson.
"""

from __future__ import annotations

from typing import Literal

import duckdb
from pydantic import BaseModel, Field

from ..models import DatasetProfile, ToolResult
from .base import Tool, ToolError, fetch_floats, require_numeric


class FitDistributionInput(BaseModel):
    column: str = Field(..., description="Numeric column to fit.")
    family: Literal["gaussian", "poisson"] = Field(..., description="Distribution family.")


class FitDistributionTool(Tool):
    name = "fit_distribution"
    description = (
        "Fit a Gaussian or Poisson distribution to a numeric column and report "
        "the fitted parameters plus a goodness-of-fit test (p-value)."
    )
    Input = FitDistributionInput

    def run(self, con, table_name, profile: DatasetProfile, inputs: FitDistributionInput) -> ToolResult:
        require_numeric(profile, inputs.column)
        try:
            import numpy as np
            from scipy import stats
        except ImportError as e:  # pragma: no cover
            raise ToolError(f"fit_distribution requires scipy/numpy: {e}") from e

        data = np.asarray(fetch_floats(con, table_name, inputs.column), dtype=float)
        n = int(data.size)
        if n < 8:
            raise ToolError(f"need at least 8 non-null values to fit, got {n}")

        if inputs.family == "gaussian":
            return self._gaussian(inputs.column, data, n, np, stats)
        return self._poisson(inputs.column, data, n, np, stats)

    def _gaussian(self, column, data, n, np, stats) -> ToolResult:
        mu = float(np.mean(data))
        sigma = float(np.std(data, ddof=1))
        stat, p = stats.normaltest(data)
        is_normal = bool(p >= 0.05)
        caveats = []
        if n < 20:
            caveats.append("small sample (n<20): normality test is low-power")
        metrics = {
            "family": "gaussian", "n": n, "mu": round(mu, 4), "sigma": round(sigma, 4),
            "statistic": round(float(stat), 4), "p_value": round(float(p), 6),
            "is_normal": is_normal,
        }
        verdict = "consistent with normal" if is_normal else "not normal"
        summary = (
            f"{column} ~ Gaussian(μ={mu:,.2f}, σ={sigma:,.2f}); D'Agostino p={p:.4f} "
            f"⇒ {verdict} at α=0.05 (n={n})."
        )
        return ToolResult(tool=self.name, summary=summary, metrics=metrics, caveats=caveats)

    def _poisson(self, column, data, n, np, stats) -> ToolResult:
        caveats = []
        if np.any(data < 0):
            raise ToolError("poisson requires non-negative values")
        if not np.allclose(data, np.round(data)):
            caveats.append("values are not integers; Poisson assumes counts")
        lam = float(np.mean(data))
        var = float(np.var(data, ddof=1))
        if lam <= 0:
            raise ToolError("poisson fit needs a positive mean")
        dispersion = var / lam
        # Dispersion (variance-to-mean) test: (n-1)*var/mean ~ chi2(n-1).
        chi2_stat = (n - 1) * dispersion
        p = float(2 * min(stats.chi2.cdf(chi2_stat, n - 1), stats.chi2.sf(chi2_stat, n - 1)))
        consistent = bool(p >= 0.05)
        metrics = {
            "family": "poisson", "n": n, "lambda": round(lam, 4),
            "variance": round(var, 4), "dispersion_index": round(dispersion, 4),
            "p_value": round(p, 6), "consistent_with_poisson": consistent,
        }
        if dispersion > 1.5:
            caveats.append("over-dispersed (variance ≫ mean): consider negative binomial")
        verdict = "consistent with Poisson" if consistent else "not Poisson (dispersion off)"
        summary = (
            f"{column} ~ Poisson(λ={lam:,.2f}); variance/mean={dispersion:.2f}, "
            f"dispersion-test p={p:.4f} ⇒ {verdict} (n={n})."
        )
        return ToolResult(tool=self.name, summary=summary, metrics=metrics, caveats=caveats)
