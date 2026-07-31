"""Regression tools: ordinary least squares and logistic regression (numpy only)."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from ..models import DatasetProfile, ToolResult
from .base import Tool, ToolError, fetch_columns, require_numeric


class LinearFitInput(BaseModel):
    target: str = Field(..., description="Numeric column to predict.")
    predictors: List[str] = Field(..., description="One or more numeric predictor columns.")


class LinearFit(Tool):
    name = "linear_fit"
    description = "Ordinary least-squares linear regression: coefficients + R² for a numeric target on numeric predictors."
    Input = LinearFitInput

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        require_numeric(profile, inputs.target)
        if not inputs.predictors:
            raise ToolError("provide at least one predictor")
        for p in inputs.predictors:
            require_numeric(profile, p)
        cols = [inputs.target] + list(inputs.predictors)
        data = fetch_columns(con, table_name, cols)
        if len(data) <= len(inputs.predictors) + 1:
            raise ToolError(f"need more rows than predictors+1 ({len(inputs.predictors)+1}), got {len(data)}")
        arr = np.asarray(data, dtype=float)
        y = arr[:, 0]
        X = np.column_stack([np.ones(len(arr)), arr[:, 1:]])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        yhat = X @ beta
        ss_res = float(np.sum((y - yhat) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
        coefs = {"intercept": round(float(beta[0]), 4)}
        for name, b in zip(inputs.predictors, beta[1:]):
            coefs[name] = round(float(b), 4)
        top = max(inputs.predictors, key=lambda p: abs(coefs[p])) if inputs.predictors else None
        return ToolResult(tool=self.name,
                          metrics={"r_squared": round(r2, 4), "n": len(data), "coefficients": coefs},
                          summary=(f"OLS {inputs.target} ~ {', '.join(inputs.predictors)}: R²={r2:.3f} "
                                   f"(n={len(data)}); largest coefficient: {top}={coefs.get(top)}."))


class LogisticFitInput(BaseModel):
    target: str = Field(..., description="Binary target column (two distinct values).")
    predictors: List[str] = Field(..., description="One or more numeric predictor columns.")


class LogisticFit(Tool):
    name = "logistic_fit"
    description = "Logistic regression (IRLS) of a binary target on numeric predictors: coefficients, pseudo-R², accuracy."
    Input = LogisticFitInput

    def run(self, con, table_name, profile, inputs):
        import numpy as np
        if not inputs.predictors:
            raise ToolError("provide at least one predictor")
        for p in inputs.predictors:
            require_numeric(profile, p)
        cols = [inputs.target] + list(inputs.predictors)
        data = fetch_columns(con, table_name, cols)
        if len(data) < 2 * (len(inputs.predictors) + 1):
            raise ToolError("not enough rows for a stable logistic fit")

        raw_y = [r[0] for r in data]
        classes = sorted({v for v in raw_y}, key=lambda v: str(v))
        if len(classes) != 2:
            raise ToolError(f"target '{inputs.target}' must be binary (2 distinct values), got {len(classes)}")
        pos = classes[1]
        y = np.array([1.0 if v == pos else 0.0 for v in raw_y])
        Xraw = np.asarray([[float(v) for v in r[1:]] for r in data], dtype=float)
        # standardize predictors for numerical stability, then map coefficients back
        mu, sd = Xraw.mean(0), Xraw.std(0)
        sd = np.where(sd == 0, 1.0, sd)
        Xs = (Xraw - mu) / sd
        X = np.column_stack([np.ones(len(Xs)), Xs])

        beta = np.zeros(X.shape[1])
        caveats: List[str] = []
        for _ in range(50):
            eta = np.clip(X @ beta, -30, 30)
            p = 1 / (1 + np.exp(-eta))
            W = np.clip(p * (1 - p), 1e-6, None)
            try:
                XtWX = X.T @ (W[:, None] * X)
                grad = X.T @ (y - p)
                step = np.linalg.solve(XtWX, grad)
            except np.linalg.LinAlgError:
                caveats.append("fit did not converge cleanly (near-singular); treat coefficients with caution")
                break
            beta = beta + step
            if np.max(np.abs(step)) < 1e-6:
                break

        eta = np.clip(X @ beta, -30, 30)
        p = 1 / (1 + np.exp(-eta))
        eps = 1e-9
        ll = float(np.sum(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))
        base = y.mean()
        ll0 = float(np.sum(y * np.log(base + eps) + (1 - y) * np.log(1 - base + eps)))
        mcfadden = 1 - ll / ll0 if ll0 else 0.0
        acc = float(np.mean((p > 0.5) == (y > 0.5)))
        # de-standardize coefficients for interpretability
        coefs = {"intercept": round(float(beta[0] - np.sum(beta[1:] * mu / sd)), 4)}
        for name, b, s in zip(inputs.predictors, beta[1:], sd):
            coefs[name] = round(float(b / s), 4)
        return ToolResult(tool=self.name, caveats=caveats,
                          metrics={"pseudo_r2_mcfadden": round(mcfadden, 4), "accuracy": round(acc, 4),
                                   "n": len(data), "positive_class": str(pos), "coefficients": coefs},
                          summary=(f"Logistic P({inputs.target}={pos}) ~ {', '.join(inputs.predictors)}: "
                                   f"pseudo-R²={mcfadden:.3f}, accuracy={acc:.2f} (n={len(data)})."))
