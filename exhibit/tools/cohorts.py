"""Customer-analytics tools: cohort retention, funnel conversion, and RFM."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from ..models import DatasetProfile, ResultTable, ToolResult
from .base import Tool, ToolError, get_column, require_numeric

_MAX_ROWS = 40


def _require_temporal(profile, name):
    col = get_column(profile, name)
    if not any(t in col.dtype.upper() for t in ("DATE", "TIMESTAMP", "TIME")):
        raise ToolError(f"'{name}' is {col.dtype}, expected a date/timestamp")


class CohortRetentionInput(BaseModel):
    entity: str = Field(..., description="Entity id column (e.g. customer_id).")
    date_column: str = Field(..., description="Activity date column.")
    granularity: Literal["day", "week", "month", "quarter", "year"] = "month"


class CohortRetention(Tool):
    name = "cohort_retention"
    description = "Cohort each entity by its first-seen period, then measure how many stay active in later periods."
    Input = CohortRetentionInput

    def run(self, con, table_name, profile, inputs):
        get_column(profile, inputs.entity)
        _require_temporal(profile, inputs.date_column)
        e, d, t, g = (f'"{inputs.entity}"', f'"{inputs.date_column}"',
                      f'"{table_name}"', inputs.granularity)
        rows = con.execute(
            f"WITH first AS (SELECT {e} AS ent, MIN(date_trunc('{g}', {d})) AS cohort "
            f"FROM {t} WHERE {d} IS NOT NULL GROUP BY 1), "
            f"act AS (SELECT DISTINCT {e} AS ent, date_trunc('{g}', {d}) AS period FROM {t}) "
            f"SELECT f.cohort, date_diff('{g}', f.cohort, a.period) AS period_offset, "
            f"COUNT(DISTINCT a.ent) AS active FROM first f JOIN act a ON f.ent = a.ent "
            f"GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        if not rows:
            raise ToolError("no activity rows found")
        sizes = {c: a for c, off, a in rows if off == 0}
        out = []
        for cohort, off, active in rows:
            size = sizes.get(cohort, 0)
            pct = round(active * 100.0 / size, 1) if size else None
            out.append([str(cohort), int(off), int(active), pct])
        # headline: average retention at offset 1
        off1 = [r[3] for r in out if r[1] == 1 and r[3] is not None]
        avg_next = round(sum(off1) / len(off1), 1) if off1 else None
        table = ResultTable(columns=["cohort", "period_offset", "active", "retention_pct"],
                            rows_preview=out[:_MAX_ROWS], row_count=len(out))
        return ToolResult(tool=self.name, table=table,
                          metrics={"cohorts": len(sizes), "avg_next_period_retention_pct": avg_next},
                          summary=(f"{len(sizes)} {g} cohorts; average retention into the next {g} "
                                   f"is {avg_next}%."))


class FunnelInput(BaseModel):
    entity: str = Field(..., description="Entity id column (e.g. user_id).")
    stage_column: str = Field(..., description="Column holding the stage/step each row represents.")
    steps: List[str] = Field(..., description="Ordered list of stage values, first to last.")


class Funnel(Tool):
    name = "funnel"
    description = "Funnel conversion: distinct entities reaching each ordered stage, with step and overall conversion rates."
    Input = FunnelInput

    def run(self, con, table_name, profile, inputs):
        get_column(profile, inputs.entity)
        get_column(profile, inputs.stage_column)
        if len(inputs.steps) < 2:
            raise ToolError("provide at least 2 ordered steps")
        e, s, t = f'"{inputs.entity}"', f'"{inputs.stage_column}"', f'"{table_name}"'
        counts = {}
        for step in inputs.steps:
            n = con.execute(
                f'SELECT COUNT(DISTINCT {e}) FROM {t} WHERE CAST({s} AS VARCHAR) = ?', [step]
            ).fetchone()[0]
            counts[step] = int(n)
        start = counts[inputs.steps[0]] or 0
        rows = []
        prev = None
        for step in inputs.steps:
            n = counts[step]
            from_start = round(n * 100.0 / start, 1) if start else None
            from_prev = round(n * 100.0 / prev, 1) if prev else None
            rows.append([step, n, from_prev, from_start])
            prev = n
        overall = rows[-1][3]
        table = ResultTable(columns=["step", "entities", "pct_of_prev", "pct_of_start"],
                            rows_preview=rows, row_count=len(rows))
        return ToolResult(tool=self.name, table=table,
                          metrics={"start_entities": start, "end_entities": rows[-1][1],
                                   "overall_conversion_pct": overall},
                          summary=(f"Funnel {inputs.steps[0]} → {inputs.steps[-1]}: "
                                   f"{start} → {rows[-1][1]} entities ({overall}% overall conversion)."))


class RfmInput(BaseModel):
    entity: str = Field(..., description="Entity id column (e.g. customer_id).")
    date_column: str = Field(..., description="Transaction date column (for recency).")
    monetary: str = Field(..., description="Numeric value column summed per entity (e.g. revenue).")


class Rfm(Tool):
    name = "rfm"
    description = "RFM analysis: score each entity 1-5 on recency, frequency, monetary; summarize the segments."
    Input = RfmInput

    def run(self, con, table_name, profile, inputs):
        get_column(profile, inputs.entity)
        _require_temporal(profile, inputs.date_column)
        require_numeric(profile, inputs.monetary)
        e, d, mo, t = (f'"{inputs.entity}"', f'"{inputs.date_column}"',
                       f'"{inputs.monetary}"', f'"{table_name}"')
        base = (
            f"WITH per AS (SELECT {e} AS ent, "
            f"date_diff('day', MAX(CAST({d} AS DATE)), (SELECT MAX(CAST({d} AS DATE)) FROM {t})) AS recency_days, "
            f"COUNT(*) AS frequency, ROUND(SUM({mo}), 2) AS monetary "
            f"FROM {t} WHERE {d} IS NOT NULL GROUP BY 1), "
            f"scored AS (SELECT ent, recency_days, frequency, monetary, "
            f"NTILE(5) OVER (ORDER BY recency_days DESC) AS r_score, "
            f"NTILE(5) OVER (ORDER BY frequency ASC) AS f_score, "
            f"NTILE(5) OVER (ORDER BY monetary ASC) AS m_score FROM per)"
        )
        n_entities = con.execute(f"{base} SELECT COUNT(*) FROM scored").fetchone()[0]
        if not n_entities:
            raise ToolError("no entities with activity found")
        # segment distribution by R/F/M score buckets
        seg = con.execute(
            f"{base} SELECT r_score || '-' || f_score || '-' || m_score AS rfm, COUNT(*) AS n "
            f"FROM scored GROUP BY 1 ORDER BY n DESC LIMIT {_MAX_ROWS}"
        ).fetchall()
        champions = con.execute(
            f"{base} SELECT COUNT(*) FROM scored WHERE r_score >= 4 AND f_score >= 4 AND m_score >= 4"
        ).fetchone()[0]
        rows = [[str(r[0]), int(r[1])] for r in seg]
        table = ResultTable(columns=["rfm_score", "entities"], rows_preview=rows, row_count=len(rows))
        return ToolResult(tool=self.name, table=table,
                          metrics={"entities": int(n_entities), "champions": int(champions),
                                   "champions_pct": round(champions * 100.0 / n_entities, 1)},
                          summary=(f"Scored {n_entities} entities on RFM; {champions} "
                                   f"({round(champions*100.0/n_entities,1)}%) are champions (R,F,M all ≥4)."))
