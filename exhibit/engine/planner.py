"""Planner stage: question -> analytical Plan (no SQL).

Thin seam over the LLM client. It exists as its own module because this is where
prompt construction and plan validation will live once a real LLM is wired in
(e.g. capping step count, ensuring steps reference real columns).
"""

from __future__ import annotations

from ..llm.base import LLMClient
from ..models import DatasetProfile, Plan


def make_plan(
    client: LLMClient, question: str, profiles, context: str = ""
) -> Plan:
    # A zero-step plan is valid: it means the question is answerable directly
    # from the deterministic profiles, so the engine skips the SQL loop and lets
    # the narrator answer from them (see orchestrator.run_question).
    return client.plan(question, profiles, context)
