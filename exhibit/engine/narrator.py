"""Narration stage: step outcomes -> interpretation, conclusion, follow-ups.

Conclusions must cite the nodes they rest on (``evidence_node_ids``) so the
investigation stays inspectable — every claim points back to a stored result.
``context`` carries prior conclusions so a follow-up answer stays thread-aware.
"""

from __future__ import annotations

from typing import List, Tuple

from ..llm.base import LLMClient
from ..models import (
    Conclusion,
    DatasetProfile,
    FollowUp,
    Interpretation,
    StepOutcome,
)


def narrate(
    client: LLMClient,
    question: str,
    profiles,
    outcomes: List[StepOutcome],
    context: str = "",
) -> Tuple[Interpretation, Conclusion, List[FollowUp]]:
    return client.narrate(question, profiles, outcomes, context)
