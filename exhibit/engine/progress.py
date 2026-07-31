"""Progress hooks for the question loop.

Each stage of ``run_question`` (planning, per-step execution, narration) is a
blocking LLM/DB call, so without feedback the terminal sits blank for many
seconds. The orchestrator emits progress events through this protocol; the REPL
supplies a live renderer, while tests (and non-interactive callers) use the
no-op ``NullProgress``. Keeping this UI-agnostic keeps the engine importable
without any terminal dependency.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Plan, PlanStep


class Progress(Protocol):
    def start_planning(self) -> None: ...
    def plan_ready(self, plan: Plan) -> None: ...
    def step_start(self, index: int, step: PlanStep) -> None: ...
    def step_done(self, index: int, step: PlanStep, ok: bool) -> None: ...
    def start_narrating(self) -> None: ...
    def phase(self, label: str) -> None:
        """Set an arbitrary phase label (used by the fast path)."""
        ...


class NullProgress:
    """No-op progress used when no UI is attached (tests, scripts)."""

    def start_planning(self) -> None:
        pass

    def plan_ready(self, plan: Plan) -> None:
        pass

    def step_start(self, index: int, step: PlanStep) -> None:
        pass

    def step_done(self, index: int, step: PlanStep, ok: bool) -> None:
        pass

    def start_narrating(self) -> None:
        pass

    def phase(self, label: str) -> None:
        pass
