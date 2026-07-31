"""Hypothesis tests and correlation tools (scipy/numpy only)."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from ..models import DatasetProfile, ResultTable, ToolResult
from .base import Tool, ToolError, fetch_columns, get_column, require_numeric


def _two_groups(con, table, group_col, value_col, a, b):
    """Fetch value lists for two groups; if a/b unset, use the 2 most frequent."""
    if a is None or b is None:
        top = con.execute(
            f'SELECT "{group_col}" FROM "{table}" WHERE "{group_col}" IS NOT NULL '
            f'GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 2'
        ).fetchall()
        if len(top) < 2:
            raise ToolError(f"column '{group_col}' has fewer than 2 groups")
        a, b = str(top[0][0]), str(top[1][0])
    va = [float(r[0]) for r in con.execute(
        f'SELECT "{value_col}" FROM "{table}" WHERE CAST("{group_col}" AS VARCHAR) = ? '
        f'AND "{value_col}" IS NOT NULL', [a]).fetchall()]
    vb = [float(r[0]) for r in con.execute(
        f'SELECT "{value_col}" FROM "{table}" WHERE CAST("{group_col}" AS VARCHAR) = ? '
        f'AND "{value_col}" IS NOT NULL', [b]).fetchall()]
    return a, b, va, vb


class _TwoGroupInput(BaseModel):
    value: str = Field(..., description="Numeric column to compare between groups.")
    group: str = Field(..., description="Categorical column defining the groups.")
    group_a: Optional[str] = Field(None, description="First group value (default: most frequent).")
    group_b: Optional[str] = Field(None, description="Second group value (default: 2nd most frequent).")


class TTest(Tool):
    name = "t_test"
    description = "Welch two-sample t-test: is a numeric measure's mean different between two groups?"
    Input = _TwoGroupInput

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        from scipy import stats
        require_numeric(profile, inputs.value)
        get_column(profile, inputs.group)
        a, b, va, vb = _two_groups(con, table_name, inputs.group, inputs.value,
                                    inputs.group_a, inputs.group_b)
        if len(va) < 2 or len(vb) < 2:
            raise ToolError(f"each group needs >=2 values (got {len(va)}, {len(vb)})")
        t, p = stats.ttest_ind(va, vb, equal_var=False)
        ma, mb = float(np.mean(va)), float(np.mean(vb))
        metrics = {"group_a": a, "group_b": b, "mean_a": round(ma, 4), "mean_b": round(mb, 4),
                   "diff": round(ma - mb, 4), "t_stat": round(float(t), 4),
                   "p_value": round(float(p), 6), "n_a": len(va), "n_b": len(vb),
                   "significant": bool(p < 0.05)}
        verdict = "significant" if p < 0.05 else "not significant"
        return ToolResult(tool=self.name, metrics=metrics, summary=(
            f"{inputs.value}: {a} mean={ma:,.2f} vs {b} mean={mb:,.2f} "
            f"(diff {ma-mb:,.2f}); Welch t={t:.2f}, p={p:.4f} ⇒ {verdict} at α=0.05."))


class MannWhitney(Tool):
    name = "mann_whitney"
    description = "Mann-Whitney U test: nonparametric check whether a numeric measure differs between two groups."
    Input = _TwoGroupInput

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        from scipy import stats
        require_numeric(profile, inputs.value)
        get_column(profile, inputs.group)
        a, b, va, vb = _two_groups(con, table_name, inputs.group, inputs.value,
                                    inputs.group_a, inputs.group_b)
        if not va or not vb:
            raise ToolError("both groups must be non-empty")
        u, p = stats.mannwhitneyu(va, vb, alternative="two-sided")
        metrics = {"group_a": a, "group_b": b, "median_a": round(float(np.median(va)), 4),
                   "median_b": round(float(np.median(vb)), 4), "u_stat": round(float(u), 2),
                   "p_value": round(float(p), 6), "n_a": len(va), "n_b": len(vb),
                   "significant": bool(p < 0.05)}
        verdict = "significant" if p < 0.05 else "not significant"
        return ToolResult(tool=self.name, metrics=metrics, summary=(
            f"{inputs.value}: median {a}={metrics['median_a']:,} vs {b}={metrics['median_b']:,}; "
            f"Mann-Whitney U={u:.0f}, p={p:.4f} ⇒ {verdict}."))


class ChiSquareInput(BaseModel):
    column_a: str = Field(..., description="First categorical column.")
    column_b: str = Field(..., description="Second categorical column.")


class ChiSquare(Tool):
    name = "chi_square"
    description = "Chi-square test of independence between two categorical columns (with Cramér's V)."
    Input = ChiSquareInput

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        from scipy import stats
        get_column(profile, inputs.column_a)
        get_column(profile, inputs.column_b)
        rows = con.execute(
            f'SELECT CAST("{inputs.column_a}" AS VARCHAR), CAST("{inputs.column_b}" AS VARCHAR), '
            f'COUNT(*) FROM "{table_name}" WHERE "{inputs.column_a}" IS NOT NULL '
            f'AND "{inputs.column_b}" IS NOT NULL GROUP BY 1, 2'
        ).fetchall()
        if not rows:
            raise ToolError("no non-null rows for the two columns")
        avals = sorted({r[0] for r in rows})
        bvals = sorted({r[1] for r in rows})
        if len(avals) < 2 or len(bvals) < 2:
            raise ToolError("both columns need >=2 distinct values for a contingency test")
        idx_a = {v: i for i, v in enumerate(avals)}
        idx_b = {v: i for i, v in enumerate(bvals)}
        table = np.zeros((len(avals), len(bvals)))
        for a, b, c in rows:
            table[idx_a[a], idx_b[b]] = c
        chi2, p, dof, expected = stats.chi2_contingency(table)
        n = table.sum()
        cramers_v = float(np.sqrt(chi2 / (n * (min(table.shape) - 1)))) if n else 0.0
        caveats = []
        if (expected < 5).mean() > 0.2:
            caveats.append(">20% of cells have expected count <5; chi-square may be unreliable")
        metrics = {"chi2": round(float(chi2), 4), "p_value": round(float(p), 6),
                   "dof": int(dof), "cramers_v": round(cramers_v, 4),
                   "significant": bool(p < 0.05)}
        assoc = "associated" if p < 0.05 else "independent"
        return ToolResult(tool=self.name, metrics=metrics, caveats=caveats, summary=(
            f"{inputs.column_a} vs {inputs.column_b}: chi²={chi2:.1f}, dof={dof}, p={p:.4f} "
            f"⇒ {assoc} at α=0.05 (Cramér's V={cramers_v:.2f})."))


class CorrelationInput(BaseModel):
    x: str = Field(..., description="First numeric column.")
    y: str = Field(..., description="Second numeric column.")
    method: Literal["pearson", "spearman"] = Field("pearson", description="Correlation method.")


class Correlation(Tool):
    name = "correlation"
    description = "Correlation between two numeric columns with a significance p-value (Pearson or Spearman)."
    Input = CorrelationInput

    def run(self, con, table_name, profile, inputs):
        from scipy import stats
        require_numeric(profile, inputs.x)
        require_numeric(profile, inputs.y)
        data = fetch_columns(con, table_name, [inputs.x, inputs.y])
        if len(data) < 3:
            raise ToolError(f"need >=3 paired non-null rows, got {len(data)}")
        xs = [float(r[0]) for r in data]
        ys = [float(r[1]) for r in data]
        fn = stats.pearsonr if inputs.method == "pearson" else stats.spearmanr
        r, p = fn(xs, ys)
        r = float(r)
        strength = ("negligible" if abs(r) < 0.1 else "weak" if abs(r) < 0.3
                    else "moderate" if abs(r) < 0.5 else "strong")
        direction = "positive" if r >= 0 else "negative"
        metrics = {"method": inputs.method, "r": round(r, 4), "p_value": round(float(p), 6),
                   "n": len(data), "significant": bool(p < 0.05)}
        return ToolResult(tool=self.name, metrics=metrics, summary=(
            f"{inputs.method} r={r:.3f} between {inputs.x} and {inputs.y} "
            f"({strength} {direction}); p={p:.4f}, n={len(data)}."))


class CorrelationMatrixInput(BaseModel):
    columns: Optional[List[str]] = Field(
        None, description="Numeric columns to correlate (default: all numeric, capped at 8).")


class CorrelationMatrix(Tool):
    name = "correlation_matrix"
    description = "Pairwise Pearson correlation matrix across numeric columns."
    Input = CorrelationMatrixInput

    _NUMERIC = ("INT", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "REAL")

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        if inputs.columns:
            cols = inputs.columns
            for c in cols:
                require_numeric(profile, c)
        else:
            cols = [c.name for c in profile.columns
                    if any(t in c.dtype.upper() for t in self._NUMERIC)][:8]
        if len(cols) < 2:
            raise ToolError("need >=2 numeric columns for a correlation matrix")
        data = fetch_columns(con, table_name, cols)
        if len(data) < 3:
            raise ToolError("need >=3 complete rows")
        arr = np.asarray(data, dtype=float)
        m = np.corrcoef(arr, rowvar=False)
        rows = [[cols[i]] + [round(float(m[i, j]), 3) for j in range(len(cols))]
                for i in range(len(cols))]
        # strongest off-diagonal pair
        best = (0.0, None, None)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                if abs(m[i, j]) > abs(best[0]):
                    best = (float(m[i, j]), cols[i], cols[j])
        table = ResultTable(columns=["column"] + cols, rows_preview=rows, row_count=len(cols))
        summary = (f"Strongest pair: {best[1]} ↔ {best[2]} (r={best[0]:.2f})."
                   if best[1] else "Correlation matrix computed.")
        return ToolResult(tool=self.name, summary=summary,
                          metrics={"columns": len(cols), "strongest_r": round(best[0], 3),
                                   "strongest_pair": f"{best[1]}~{best[2]}" if best[1] else None},
                          table=table)
