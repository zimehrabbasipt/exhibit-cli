"""SQL-generation stage: one PlanStep -> read-only DuckDB SQL.

Kept separate from the planner so the model that *describes* an analysis is not
the same call that *writes* SQL — this makes SQL easier to validate and replay.
"""

from __future__ import annotations

from ..llm.base import LLMClient
from ..models import PlanStep, SqlQuery


def generate_sql(client: LLMClient, step: PlanStep, profiles, prior_outcomes=()) -> SqlQuery:
    query = client.generate_sql(step, profiles, prior_outcomes)
    if not query.sql.strip():
        raise ValueError(f"SQL generation returned empty SQL for step {step.id}")
    return query
