"""The ``LLMClient`` protocol the engine depends on.

We expose *domain* methods (plan / generate_sql / generate_tool_call / narrate)
rather than a single generic ``complete`` call. This keeps the engine
declarative and lets a real adapter build stage-specific prompts internally
while returning validated Pydantic models. Each method's return type is a
structured contract from ``models.py`` — never free text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Protocol, Tuple, runtime_checkable

from ..models import (
    Conclusion,
    DatasetProfile,
    FollowUp,
    Interpretation,
    JudgeReview,
    Plan,
    PlanStep,
    StepOutcome,
    ToolCall,
    SqlQuery,
)

if TYPE_CHECKING:  # avoid a hard llm -> tools import at runtime
    from ..tools.base import Tool


@runtime_checkable
class LLMClient(Protocol):
    # Whether this backend can run the agentic fast path (single run_sql loop).
    supports_fast_path: bool

    @property
    def name(self) -> str:
        """Identifier stored on nodes for provenance (e.g. 'mock', 'claude-...')."""
        ...

    def investigate(
        self,
        question: str,
        profiles: List[DatasetProfile],
        context: str,
        run_sql: Callable[[str], str],
    ) -> str:
        """Fast path: answer the question in a single agentic loop, calling
        ``run_sql`` (which executes read-only SQL and returns rows as text) as
        needed, then returning a final evidence-backed answer. Only called when
        ``supports_fast_path`` is True."""
        ...

    def plan(
        self, question: str, profiles: List[DatasetProfile], context: str = ""
    ) -> Plan:
        """Turn a user question into steps. A step is executed either as SQL or by
        calling a named analysis tool (``step.method``). An empty plan means the
        question is answerable directly from the profiles. ``profiles`` is the full
        table catalog (one per loaded table); ``context`` carries a compact digest
        of the investigation so far (prior Q&A + recent results)."""
        ...

    def generate_sql(
        self,
        step: PlanStep,
        profiles: List[DatasetProfile],
        prior_outcomes: List[StepOutcome] = (),
    ) -> SqlQuery:
        """Translate one analytical step into read-only DuckDB SQL (may JOIN across
        the tables in ``profiles``). ``prior_outcomes`` are the results of earlier
        steps in the same question, so a dependent step can reuse/join on them
        instead of recomputing a broader scope."""
        ...

    def generate_tool_call(
        self, step: PlanStep, profiles: List[DatasetProfile], tool: "Tool"
    ) -> ToolCall:
        """Produce validated arguments for a chosen analysis tool. The tool is
        single-table, but its columns may come from any ONE of the loaded ``profiles``
        (the engine runs it on whichever table holds them)."""
        ...

    def narrate(
        self,
        question: str,
        profiles: List[DatasetProfile],
        outcomes: List[StepOutcome],
        context: str = "",
    ) -> Tuple[Interpretation, Conclusion, List[FollowUp]]:
        """Interpret step outcomes (SQL tables and/or tool results) into findings,
        a conclusion, and follow-ups. With no outcomes, answer from the profile.
        ``context`` carries prior conclusions so the answer stays thread-aware."""
        ...

    def summarize(self, prior_summary: str, turns: List[Tuple[str, str]]) -> str:
        """Fold older ``(question, conclusion)`` turns into a compact running
        summary of the investigation (kept bounded so long investigations retain
        early findings without re-sending every turn)."""
        ...

    def critique(self, transcript: str) -> "JudgeReview":
        """Adversarially review an entire investigation ``transcript`` and return a
        structured, skeptical critique (assumptions, weakly-supported conclusions,
        missing evidence, untested alternatives, a simpler-explanation check, a
        confidence assessment, and the single most valuable next query). Default to
        skepticism. Never invent node ids."""
        ...
