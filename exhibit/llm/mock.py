"""Deterministic mock LLM.

This lets the entire pipeline run offline and be unit-tested without an API key.
It is not "AI" — it uses simple, transparent heuristics over the dataset profile
to pick a date column and a numeric measure, emit a monthly-trend query, and
narrate the result table. Its job is to exercise every seam (plan -> sql ->
execute -> narrate -> persist) so the real adapter is a drop-in replacement.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..models import (
    Alternative,
    Claim,
    ColumnProfile,
    Conclusion,
    DatasetProfile,
    FollowUp,
    Interpretation,
    JudgeReview,
    Plan,
    PlanStep,
    ResultTable,
    SqlQuery,
    WeakClaim,
)

_DATE_TYPES = ("DATE", "TIMESTAMP", "TIME")
_NUMERIC_TYPES = ("INT", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "REAL")
_MEASURE_HINTS = ("revenue", "sales", "amount", "total", "profit", "value")
_DIMENSION_HINTS = ("segment", "region", "product", "category", "channel", "country")


class MockLLM:
    name = "mock"
    supports_fast_path = False   # the deterministic mock always uses the full path

    def __init__(self) -> None:
        from .usage import Usage

        self.usage = Usage()        # stays zero — the mock makes no API calls
        self.pricing_model = None

    def investigate(self, question, profiles, context, run_sql):  # pragma: no cover
        raise NotImplementedError("MockLLM does not support the agentic fast path")

    # -- planning ---------------------------------------------------------- #
    def plan(self, question: str, profiles, context: str = "") -> Plan:
        # The deterministic mock ignores context and multi-table joins; it plans
        # over the primary (first) table only. Real cross-table reasoning is a
        # Claude-backend feature.
        profile = profiles[0]
        measure = self._measure_column(profile)
        date = self._date_column(profile)
        if date and measure:
            step = PlanStep(
                id="s1",
                intent="measure_trend_over_time",
                description=(
                    f"Aggregate {measure.name} by month using {date.name} to reveal "
                    "the trend and locate any decline."
                ),
                expected_output="table",
            )
        elif measure:
            dim = self._dimension_column(profile)
            target = dim.name if dim else profile.columns[0].name
            step = PlanStep(
                id="s1",
                intent="measure_by_dimension",
                description=f"Aggregate {measure.name} grouped by {target}.",
                expected_output="table",
            )
        else:
            step = PlanStep(
                id="s1",
                intent="preview_rows",
                description="Preview representative rows from the dataset.",
                expected_output="table",
            )
        # Batch mode: inline the SQL into the step so the plan carries the command.
        step.sql = self.generate_sql(step, profiles).sql
        return Plan(
            question=question,
            rationale=(
                "Establish the top-level trend first; drill into contributing "
                "dimensions as follow-ups."
            ),
            steps=[step],
        )

    # -- sql generation ---------------------------------------------------- #
    def generate_sql(self, step: PlanStep, profiles, prior_outcomes=()) -> SqlQuery:
        # The mock plans single-step, single-table queries, so it ignores prior
        # steps; cross-step composition is a Claude-backend feature.
        profile = profiles[0]
        tbl = f'"{profile.table_name}"'
        measure = self._measure_column(profile)
        date = self._date_column(profile)

        if step.intent == "measure_trend_over_time" and date and measure:
            sql = (
                f'SELECT date_trunc(\'month\', "{date.name}") AS month, '
                f'ROUND(SUM("{measure.name}"), 2) AS total_{_slug(measure.name)} '
                f"FROM {tbl} "
                f'WHERE "{date.name}" IS NOT NULL '
                f"GROUP BY 1 ORDER BY 1"
            )
            return SqlQuery(
                step_id=step.id,
                sql=sql,
                reads_columns=[date.name, measure.name],
                notes="Monthly aggregation of the primary measure.",
            )

        if step.intent == "measure_by_dimension" and measure:
            dim = self._dimension_column(profile) or profile.columns[0]
            sql = (
                f'SELECT "{dim.name}", ROUND(SUM("{measure.name}"), 2) AS total_{_slug(measure.name)} '
                f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC"
            )
            return SqlQuery(
                step_id=step.id,
                sql=sql,
                reads_columns=[dim.name, measure.name],
                notes="Measure grouped by primary dimension.",
            )

        return SqlQuery(
            step_id=step.id,
            sql=f"SELECT * FROM {tbl}",
            reads_columns=[c.name for c in profile.columns],
            notes="Row preview (guard will apply a LIMIT).",
        )

    # -- tool arg generation (mock never selects tools) -------------------- #
    def generate_tool_call(self, step, profiles, tool):
        raise NotImplementedError(
            "MockLLM does not select tools; use --llm anthropic for tool selection"
        )

    # -- narration --------------------------------------------------------- #
    def narrate(
        self,
        question: str,
        profiles,
        outcomes: List["StepOutcome"],
        context: str = "",
    ) -> Tuple[Interpretation, Conclusion, List[FollowUp]]:
        profile = profiles[0]
        findings: List[str] = []
        evidence: List[str] = []
        summary = "Completed the analysis; see the result table(s) for detail."
        confidence = "low"

        for outcome in outcomes:
            step = outcome.step
            evidence.append(outcome.node_id)
            if outcome.tool_result is not None:
                findings.append(f"{outcome.tool_result.tool}: {outcome.tool_result.summary}")
                summary = outcome.tool_result.summary
                confidence = "medium"
                continue
            table = outcome.table
            if table is None:
                continue
            trend = _numeric_trend(table)
            if trend is None:
                findings.append(
                    f"Step '{step.intent}' returned {table.row_count} rows across "
                    f"columns {', '.join(table.columns)}."
                )
                continue
            label_col, value_col, low_label, low_val, high_label, high_val, last_label, last_val = trend
            findings.append(
                f"Lowest {value_col} was {_fmt(low_val)} at {low_label}; highest was "
                f"{_fmt(high_val)} at {high_label} (by {label_col})."
            )
            findings.append(
                f"Most recent period {last_label} had {value_col} = {_fmt(last_val)}."
            )
            summary = (
                f"{value_col} bottomed out at {_fmt(low_val)} ({low_label}) versus a "
                f"peak of {_fmt(high_val)} ({high_label}), a "
                f"{_pct_drop(high_val, low_val)} swing."
            )
            confidence = "medium"

        interpretation = Interpretation(findings=findings, evidence_node_ids=evidence)
        conclusion = Conclusion(
            summary=summary, confidence=confidence, evidence_node_ids=evidence
        )
        follow_ups = self._follow_ups(profile)
        return interpretation, conclusion, follow_ups

    # -- rolling summary (deterministic) ----------------------------------- #
    def summarize(self, prior_summary, turns):
        parts = [prior_summary] if prior_summary else []
        parts += [f"{q} => {c}" for q, c in turns]
        return " | ".join(parts)[:1500]

    def critique(self, transcript: str) -> JudgeReview:
        """Deterministic stand-in critique so the judge pipeline runs offline. Not
        'AI' — a fixed, transparent skeleton that exercises the review node path."""
        return JudgeReview(
            overall="Deterministic mock review: the pipeline ran; a real backend "
                    "supplies substantive critique.",
            claims=[Claim(text="The headline finding.", claim_type="descriptive",
                          verdict="weak",
                          why="Mock backend does not decompose; use --llm anthropic.")],
            assumptions=["The chosen measure and date column are the right ones for "
                         "this question."],
            weak_conclusions=[WeakClaim(
                claim="The headline conclusion.",
                why="Mock backend cannot judge support; use --llm anthropic for a real "
                    "critique.")],
            missing_evidence=["A breakdown by a second dimension to rule out confounds."],
            untested_alternatives=[Alternative(
                hypothesis="A composition/mix shift, not a real change.",
                how_to_test="Decompose the change by the largest categorical dimension.")],
            simpler_explanation=None,
            confidence_assessment="Cannot assess with the mock backend.",
            next_query=FollowUp(
                question="What does the result look like split by the main dimension?",
                why="Guards against a Simpson's-paradox / mix effect."),
        )

    # -- follow-ups -------------------------------------------------------- #
    def _follow_ups(self, profile: DatasetProfile) -> List[FollowUp]:
        out: List[FollowUp] = []
        measure = self._measure_column(profile)
        for col in profile.columns:
            if _matches(col.name, _DIMENSION_HINTS):
                out.append(
                    FollowUp(
                        question=f"Which {col.name} contributed most to the change?",
                        why=f"{col.name} is a categorical dimension worth decomposing.",
                    )
                )
            if len(out) >= 3:
                break
        if measure:
            out.append(
                FollowUp(
                    question=(
                        f"Was the change driven by order volume or by average "
                        f"{measure.name} per order?"
                    ),
                    why="Separates a volume effect from a price/mix effect.",
                )
            )
        return out[:3]

    # -- column heuristics ------------------------------------------------- #
    def _date_column(self, profile: DatasetProfile) -> Optional[ColumnProfile]:
        for c in profile.columns:
            if any(t in c.dtype.upper() for t in _DATE_TYPES):
                return c
        for c in profile.columns:
            if "date" in c.name.lower():
                return c
        return None

    def _measure_column(self, profile: DatasetProfile) -> Optional[ColumnProfile]:
        numeric = [c for c in profile.columns if _is_numeric(c) and not _is_id(c)]
        for c in numeric:
            if _matches(c.name, _MEASURE_HINTS):
                return c
        return numeric[0] if numeric else None

    def _dimension_column(self, profile: DatasetProfile) -> Optional[ColumnProfile]:
        for c in profile.columns:
            if _matches(c.name, _DIMENSION_HINTS):
                return c
        # Fall back to a low-cardinality non-id column.
        for c in profile.columns:
            if not _is_id(c) and (c.distinct_count or 0) <= max(1, profile.row_count // 4):
                if not _is_numeric(c):
                    return c
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _is_numeric(col: ColumnProfile) -> bool:
    return any(t in col.dtype.upper() for t in _NUMERIC_TYPES)


def _is_id(col: ColumnProfile) -> bool:
    return bool(re.search(r"(^|_)id($|_)", col.name.lower())) or col.name.lower().endswith("id")


def _matches(name: str, hints: Tuple[str, ...]) -> bool:
    low = name.lower()
    return any(h in low for h in hints)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "value"


def _numeric_trend(table: ResultTable):
    """Return (label_col, value_col, low_label, low_val, high_label, high_val,
    last_label, last_val) for a 2+ column table whose last column is numeric."""
    if len(table.columns) < 2 or not table.rows_preview:
        return None
    value_idx = len(table.columns) - 1
    label_idx = 0
    numeric_rows = []
    for row in table.rows_preview:
        val = row[value_idx]
        try:
            numeric_rows.append((str(row[label_idx]), float(val)))
        except (TypeError, ValueError):
            return None
    if not numeric_rows:
        return None
    low = min(numeric_rows, key=lambda r: r[1])
    high = max(numeric_rows, key=lambda r: r[1])
    last = numeric_rows[-1]
    return (
        table.columns[label_idx],
        table.columns[value_idx],
        low[0], low[1],
        high[0], high[1],
        last[0], last[1],
    )


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}"


def _pct_drop(high: float, low: float) -> str:
    if high == 0:
        return "n/a"
    return f"{(high - low) / abs(high) * 100:.0f}%"
