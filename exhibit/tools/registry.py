"""Tool registry — the catalog the LLM selects from (and users invoke via /tool).

SQL execution stays its own path in the engine; the registry holds the
computational tools that go beyond what SQL expresses cleanly.
"""

from __future__ import annotations

from typing import Dict, List

from .base import Tool
from .cohorts import CohortRetention, Funnel, Rfm
from .decompose_contribution import DecomposeContributionTool
from .describe import DescribeTool
from .fit_distribution import FitDistributionTool
from .modeling import LinearFit, LogisticFit
from .outliers import OutliersTool
from .stats_tests import (
    ChiSquare,
    Correlation,
    CorrelationMatrix,
    MannWhitney,
    TTest,
)
from .timeseries import GrowthRates, MovingAverage, Trend
from .volume_vs_rate import VolumeVsRateTool

_TOOLS: List[Tool] = [
    # summary / detection
    DescribeTool(),
    OutliersTool(),
    DecomposeContributionTool(),
    VolumeVsRateTool(),
    FitDistributionTool(),
    # hypothesis tests + correlation
    TTest(),
    MannWhitney(),
    ChiSquare(),
    Correlation(),
    CorrelationMatrix(),
    # time series
    MovingAverage(),
    GrowthRates(),
    Trend(),
    # modeling
    LinearFit(),
    LogisticFit(),
    # customer analytics
    CohortRetention(),
    Funnel(),
    Rfm(),
]

REGISTRY: Dict[str, Tool] = {t.name: t for t in _TOOLS}


def get_tool(name: str) -> Tool:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown tool '{name}'. Available: {', '.join(sorted(REGISTRY))}"
        )


def tool_specs() -> List[dict]:
    """Machine-readable specs for LLM tool selection / function calling."""
    return [t.spec() for t in _TOOLS]
