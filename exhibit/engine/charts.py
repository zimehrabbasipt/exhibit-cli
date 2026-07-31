"""Chart rendering — polished PNG artifacts + a terminal preview.

Styling follows the dataviz design system's validated reference palette (see the
dataviz skill): a clean chart surface, hidden top/right spines, a hairline y-grid
behind the data, muted ticks, the categorical series colors in fixed order, and
selective value labels — so the output reads as designed rather than default
matplotlib. Charts are read by people; the look is part of the analysis.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from ..models import ResultTable

# --- validated reference palette (light surface) --------------------------- #
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
# categorical hues in fixed order (never cycled arbitrarily)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

# --- dark surface theme (for the dark HTML viewer) ------------------------- #
DARK = {
    "surface": "#17171b", "ink": "#f1f1ef", "ink2": "#c9c8c4",
    "muted": "#8f8e88", "grid": "#2b2b31", "baseline": "#3c3c43",
    # brighter, CVD-safe-ordered hues that read on a dark surface
    "series": ["#5b9cf0", "#ff8f5e", "#3ed6a1", "#ffcb5c",
               "#f294bb", "#43c76a", "#8b7be8", "#ff6f6a"],
}
LIGHT = {
    "surface": SURFACE, "ink": INK, "ink2": INK_SECONDARY,
    "muted": MUTED, "grid": GRID, "baseline": BASELINE, "series": SERIES,
}

CHART_TYPES = ("line", "bar", "scatter", "histogram")


def _fmt(v: float) -> str:
    if v is None:
        return ""
    if abs(v - round(v)) < 1e-9 and abs(v) >= 1000:
        return f"{int(round(v)):,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _col_index(table: ResultTable, name: str) -> int:
    if name not in table.columns:
        raise ValueError(f"column '{name}' not in result (have: {', '.join(table.columns)})")
    return table.columns.index(name)


def _floats(rows, i):
    return [None if r[i] is None else float(r[i]) for r in rows]


def render_png(
    table: ResultTable,
    chart_type: str,
    x: str,
    y: Optional[str],
    title: str,
    out_path: Path,
    dark: bool = False,
) -> Path:
    """Render a designed PNG chart from a result table to ``out_path``. ``dark`` swaps in
    the dark-surface theme (for the dark HTML viewer)."""
    if chart_type not in CHART_TYPES:
        raise ValueError(f"unknown chart_type {chart_type!r} (use {', '.join(CHART_TYPES)})")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = table.rows_preview
    if not rows:
        raise ValueError("nothing to chart (empty result)")

    t = DARK if dark else LIGHT
    surf, ink, ink2, s0 = t["surface"], t["ink"], t["ink2"], t["series"][0]

    with plt.rc_context(_RC):
        fig, ax = plt.subplots(figsize=(7.2, 4.2))
        fig.set_facecolor(surf)
        ax.set_facecolor(surf)

        if chart_type == "histogram":
            vals = [v for v in _floats(rows, _col_index(table, x)) if v is not None]
            ax.hist(vals, bins=min(20, max(5, len(vals) // 2 or 5)),
                    color=s0, edgecolor=surf, linewidth=0.8, zorder=3)
            ax.set_xlabel(x, color=ink2)
            ax.set_ylabel("count", color=ink2)
        else:
            yi = _col_index(table, y)
            ys = _floats(rows, yi)
            if chart_type == "scatter":
                xs = _floats(rows, _col_index(table, x))
                ax.scatter(xs, ys, s=46, color=s0, edgecolor=surf,
                           linewidth=0.6, alpha=0.85, zorder=3)
                ax.set_xlabel(x, color=ink2)
            else:
                labels = [str(r[_col_index(table, x)]) for r in rows]
                idx = list(range(len(labels)))
                if chart_type == "line":
                    ax.plot(idx, ys, color=s0, linewidth=2, marker="o",
                            markersize=5, markerfacecolor=s0,
                            markeredgecolor=surf, markeredgewidth=1, zorder=3)
                else:  # bar
                    bars = ax.bar(idx, ys, width=0.68, color=s0, zorder=3)
                    if len(labels) <= 12:
                        ax.bar_label(bars, labels=[_fmt(v) for v in ys], padding=3,
                                     color=ink2, fontsize=8)
                ax.set_xticks(idx)
                rot = 35 if (len(labels) > 6 or max((len(s) for s in labels), default=0) > 6) else 0
                ax.set_xticklabels(labels, rotation=rot, ha="right" if rot else "center")
            ax.set_ylabel(y, color=ink2)

        _style_axes(ax, t)
        ax.set_title(title, loc="left", color=ink, fontsize=13, fontweight="semibold", pad=12)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(out_path, dpi=130, facecolor=surf)
        plt.close(fig)
    return out_path


_RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Arial", "DejaVu Sans"],
    "axes.grid": True,
    "axes.axisbelow": True,
}


def _style_axes(ax, t=LIGHT) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["baseline"])
        ax.spines[side].set_linewidth(0.8)
    ax.grid(axis="y", color=t["grid"], linewidth=0.8)
    ax.grid(axis="x", visible=False)
    ax.tick_params(colors=t["muted"], length=0, labelsize=9)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(t["muted"])


# --- heuristic: is a result worth charting, and how? ----------------------- #

_TEMPORAL_HINT = ("date", "month", "period", "year", "week", "quarter", "day")


def suggest_chart(table: ResultTable) -> Optional[dict]:
    """Propose a chart spec for a result table, or None if it isn't chartable.
    Charts the label (first) column against the numeric (last) column."""
    if len(table.columns) < 2 or not table.rows_preview:
        return None
    if not (2 <= table.row_count <= 60):
        return None
    yi = len(table.columns) - 1
    try:
        for r in table.rows_preview:
            if r[yi] is not None:
                float(r[yi])
    except (TypeError, ValueError):
        return None
    x, y = table.columns[0], table.columns[yi]
    x0 = str(table.rows_preview[0][0])
    temporal = bool(re.match(r"^\d{4}-\d{2}", x0)) or any(h in x.lower() for h in _TEMPORAL_HINT)
    return {"chart_type": "line" if temporal else "bar", "x": x, "y": y, "title": f"{y} by {x}"}


def terminal_preview(table: ResultTable, chart_type: str, x: str, y: Optional[str]) -> None:
    """Best-effort inline terminal chart (plotext). Never raises."""
    try:
        import plotext as plt
        rows = table.rows_preview
        plt.clear_figure()
        plt.plotsize(70, 18)
        plt.theme("clear")
        if chart_type == "histogram":
            vals = [float(r[_col_index(table, x)]) for r in rows if r[_col_index(table, x)] is not None]
            plt.hist(vals, bins=15)
        elif chart_type == "scatter":
            xs = [float(r[_col_index(table, x)]) for r in rows]
            ys = [float(r[_col_index(table, y)]) for r in rows]
            plt.scatter(xs, ys)
        else:
            labels = [str(r[_col_index(table, x)]) for r in rows]
            ys = [float(r[_col_index(table, y)]) if r[_col_index(table, y)] is not None else 0.0 for r in rows]
            if chart_type == "line":
                plt.plot(ys)
            else:
                plt.bar(labels, ys)
        plt.title(x if y is None else f"{y} by {x}")
        plt.show()
    except Exception:
        pass
