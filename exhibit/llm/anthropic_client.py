"""Real LLM adapter backed by Anthropic Claude.

Implements the same ``LLMClient`` protocol as ``MockLLM`` using the official
``anthropic`` SDK's structured-output helper (``messages.parse`` with
``output_format=<Pydantic model>``), so planning, SQL generation, and narration
return validated Pydantic objects — no manual JSON parsing. Defaults to
``claude-opus-4-8`` with adaptive thinking.

The SDK is imported lazily so the rest of Exhibit runs without it installed.
"""

from __future__ import annotations

from typing import List, Tuple

from typing import TYPE_CHECKING

from ..config import PROMPT_VERSION
from ..models import (
    Conclusion,
    DatasetProfile,
    FollowUp,
    Interpretation,
    JudgeReview,
    NarrationResponse,
    Plan,
    PlanStep,
    StepOutcome,
    ToolCall,
    SqlQuery,
)
from . import prompts
from .usage import Usage

if TYPE_CHECKING:
    from ..tools.base import Tool

DEFAULT_MODEL = "claude-opus-4-8"
CHEAP_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 8192
_FAST_MAX_ITERS = 6  # tool rounds in the fast-path agentic loop

# Model routing: mechanical steps whose output is *externally verified* (SQL runs under
# the read-only guard in DuckDB; tool args validate against the tool's schema; summaries
# are lossy-by-design) go to the cheap model. Judgment steps that aren't cheaply checkable
# — planning, interpretation, adversarial critique — stay on the frontier model. Routing
# down is only safe *because* Exhibit already validates the mechanical outputs.
_MECHANICAL = {"generate_sql", "generate_tool_call", "summarize"}
_JUDGMENT = {"plan", "narrate", "critique", "investigate"}


class AnthropicLLM:
    """LLMClient implementation over Anthropic Claude."""

    supports_fast_path = True

    def __init__(self, model: str = DEFAULT_MODEL, cheap_model: str = CHEAP_MODEL,
                 effort: str = "high", route: bool = True):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - import guard
            raise RuntimeError(
                "The 'anthropic' package is required for the Claude backend. "
                "Install it with: pip install anthropic"
            ) from e
        self._client = anthropic.Anthropic()  # resolves creds from env / ant profile
        self._model = model
        self._cheap = cheap_model if route else model
        self._effort = effort
        self._route = route
        self.usage = Usage()          # accumulated across this session's calls
        self.pricing_model = model    # smart model (per-model cost still tracked in usage)

    def _model_for(self, method: str) -> str:
        """The model this method routes to: cheap for mechanical, frontier for judgment."""
        return self._cheap if method in _MECHANICAL else self._model

    @property
    def name(self) -> str:
        if self._route and self._cheap != self._model:
            return f"anthropic:{self._model}+{self._cheap}"
        return f"anthropic:{self._model}"

    # -- transport -------------------------------------------------------- #
    def _parse(self, system: str, user: str, schema, effort: str = None, model: str = None):
        # The system block holds the stable stage instruction + dataset catalog;
        # cache_control lets repeated calls in an investigation read it (~0.1x)
        # instead of re-paying for the whole catalog each call. Caching only kicks
        # in above the model's minimum prefix (~4k tokens), so small single-table
        # catalogs simply won't cache — no downside.
        m = model or self._model
        response = self._client.messages.parse(
            model=m,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": effort or self._effort},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        self.usage.record(response.usage, m)
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError(
                f"Claude returned no structured output (stop_reason="
                f"{getattr(response, 'stop_reason', '?')})"
            )
        return parsed

    # -- LLMClient -------------------------------------------------------- #
    def plan(
        self, question: str, profiles, context: str = ""
    ) -> Plan:
        system, user = prompts.build_plan_messages(question, profiles, context)
        plan: Plan = self._parse(system, user, Plan)
        plan.question = question  # keep the exact question the user asked
        return plan

    def generate_sql(self, step: PlanStep, profiles, prior_outcomes=()) -> SqlQuery:
        system, user = prompts.build_sql_messages(step, profiles, prior_outcomes)
        query: SqlQuery = self._parse(system, user, SqlQuery, effort="medium",
                                      model=self._model_for("generate_sql"))
        query.step_id = step.id
        return query

    def generate_tool_call(
        self, step: PlanStep, profiles, tool: "Tool"
    ) -> ToolCall:
        system, user = prompts.build_tool_args_messages(step, profiles, tool)
        # Use the tool's own Pydantic Input as the structured-output schema, so
        # the model returns arguments already validated against that tool.
        parsed = self._parse(system, user, tool.Input, effort="medium",
                             model=self._model_for("generate_tool_call"))
        return ToolCall(tool=tool.name, inputs=parsed.model_dump())

    def investigate(self, question: str, profiles, context: str, run_sql) -> str:
        system, user = prompts.build_investigate_messages(question, profiles, context)
        tools = [{
            "name": "run_sql",
            "description": "Run a read-only DuckDB SELECT over the tables; returns rows as text.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string",
                                         "description": "A single read-only SELECT (WITH/JOINs allowed)."}},
                "required": ["query"],
            },
        }]
        messages = [{"role": "user", "content": user}]
        final = ""
        for _ in range(_FAST_MAX_ITERS):
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                messages=messages,
            )
            self.usage.record(resp.usage, self._model)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "tool_use":
                results = []
                for blk in resp.content:
                    if getattr(blk, "type", None) == "tool_use" and blk.name == "run_sql":
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": blk.id,
                            "content": run_sql(blk.input.get("query", "")),
                        })
                messages.append({"role": "user", "content": results})
                continue
            final = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            break
        return final

    def summarize(self, prior_summary: str, turns) -> str:
        system, user = prompts.build_summary_messages(prior_summary, list(turns))
        m = self._model_for("summarize")
        resp = self._client.messages.create(
            model=m,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self.usage.record(resp.usage, m)
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    def critique(self, transcript: str) -> "JudgeReview":
        system, user = prompts.build_review_messages(transcript)
        return self._parse(system, user, JudgeReview)

    def narrate(
        self,
        question: str,
        profiles,
        outcomes: List[StepOutcome],
        context: str = "",
    ) -> Tuple[Interpretation, Conclusion, List[FollowUp]]:
        system, user = prompts.build_narrate_messages(question, profiles, outcomes, context)
        resp: NarrationResponse = self._parse(system, user, NarrationResponse)
        evidence = [o.node_id for o in outcomes]
        interpretation = Interpretation(findings=resp.findings, evidence_node_ids=evidence)
        conclusion = Conclusion(
            summary=resp.summary, confidence=resp.confidence, evidence_node_ids=evidence
        )
        return interpretation, conclusion, list(resp.follow_ups)


# Provenance note: nodes produced via this client record `model=name` and
# `prompt_version=PROMPT_VERSION` (set by the orchestrator), so a persisted
# investigation records exactly which model/prompt produced each step.
_ = PROMPT_VERSION
