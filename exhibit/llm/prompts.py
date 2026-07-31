"""Prompt construction for the real LLM adapter.

Kept as pure functions (no SDK import) so they're unit-testable offline and the
adapter stays a thin transport layer. Each builder returns ``(system, user)``,
split for prompt caching:

- ``system`` holds the STABLE content for the investigation — the stage
  instruction plus the (multi-table) dataset catalog. It's byte-identical across
  calls of the same stage, so the adapter marks it ``cache_control: ephemeral``
  and repeated calls read it from cache (~0.1x cost) instead of re-paying for the
  whole catalog every time.
- ``user`` holds the VOLATILE content — the specific question/step, prior-step
  results, and cross-question context — which changes each call and isn't cached.

The deterministic dataset profile/catalog is always included so the model reasons
over real schema/columns instead of guessing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

from ..models import DatasetProfile, PlanStep, ResultTable, StepOutcome
from ..tools import tool_specs

if TYPE_CHECKING:
    from ..tools.base import Tool

_MAX_PREVIEW_ROWS = 20


def format_catalog(profiles: List[DatasetProfile]) -> str:
    """Render one or more table profiles for the LLM. With multiple tables, the
    model is told it may join across them."""
    if len(profiles) == 1:
        return format_profile(profiles[0])
    blocks = [
        "Tables available — you may JOIN across them; infer join keys from shared "
        "column names (e.g. customer_id, product):",
        "",
    ]
    for p in profiles:
        blocks.append(format_profile(p))
        blocks.append("")
    return "\n".join(blocks).rstrip()


def format_profile(profile: DatasetProfile) -> str:
    lines = [f"Table `{profile.table_name}` — {profile.row_count:,} rows. Columns:"]
    for c in profile.columns:
        bits = [f"  - {c.name} ({c.dtype})"]
        if c.distinct_count is not None:
            bits.append(f"{c.distinct_count} distinct")
        # Always report null rate (including 0%) so completeness questions are
        # answerable directly from the profile.
        bits.append(f"{c.null_fraction * 100:.1f}% null")
        if c.min is not None or c.max is not None:
            bits.append(f"range [{c.min}, {c.max}]")
        if c.sample_values:
            bits.append("e.g. " + ", ".join(c.sample_values[:3]))
        lines.append(bits[0] + " — " + "; ".join(bits[1:]) if len(bits) > 1 else bits[0])
    return "\n".join(lines)


def _format_table(t: ResultTable) -> str:
    header = " | ".join(t.columns)
    rows = [
        " | ".join("" if v is None else str(v) for v in row)
        for row in t.rows_preview[:_MAX_PREVIEW_ROWS]
    ]
    out = [header, *rows]
    if t.row_count > min(len(t.rows_preview), _MAX_PREVIEW_ROWS):
        out.append(f"... ({t.row_count} rows total)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #

PLAN_SYSTEM = (
    "You are a senior data analyst. Given a question, the table catalog, and the "
    "available tools, produce an ordered plan of 1-4 steps that TOGETHER answer it "
    "— and emit the EXACT executable command for each step (the engine runs them "
    "in order, deterministically, with no further help from you). Establish the "
    "headline first, then decompose.\n\n"
    "For each step set a method and its command:\n"
    "  - method='sql': put ONE read-only DuckDB SELECT in the `sql` field (a "
    "leading WITH is allowed). Use only existing tables/columns; you may JOIN "
    "(choose join types deliberately — INNER to keep matches, LEFT / LEFT…IS NULL "
    "for absence). Round aggregates to 2 decimals. To build on an earlier step, "
    "inline that step's query as a CTE/subquery (you wrote it above) rather than "
    "recomputing a different scope. Never write/ATTACH/COPY or read files.\n"
    "  - method='tool': set `tool` to a listed tool name and put its arguments as "
    "a compact JSON object string in `tool_args_json` (matching that tool's "
    "parameters). Tools read ONLY real columns, and ALL of a tool's columns must live "
    "on the SAME single table — it may be any loaded table, not just the first; the "
    "engine runs the tool on whichever table holds those columns. If the columns you "
    "need are on different tables, or are computed in another step, use method='sql' "
    "(join/derive there) instead. Prefer a tool when it directly matches (dimension "
    "decomposition, volume-vs-value, distribution fit, outliers, column summary).\n\n"
    "When a step BUILDS ON an earlier step (reuses its result as a CTE/subquery or "
    "scopes to the set it found), list that earlier step's id in the step's "
    "`depends_on`. Leave it empty for independent steps — it records real analytical "
    "dependency, not mere ordering.\n\n"
    "The profile is authoritative for completeness/nulls, dtypes, value ranges, "
    "and cardinality — if the question is fully answerable from it, return an "
    "EMPTY steps list (no commands). If prior investigation context is provided, "
    "build on it and don't recompute established facts."
)


def format_tool_catalog() -> str:
    lines = ["Available analysis tools:"]
    for spec in tool_specs():
        params = ", ".join(spec["input_schema"].get("properties", {}).keys())
        lines.append(f"  - {spec['name']}: {spec['description']} (args: {params})")
    return "\n".join(lines)


def build_plan_messages(
    question: str, profiles: List[DatasetProfile], context: str = ""
) -> Tuple[str, str]:
    # stable (cached): stage instruction + table catalog + tool catalog
    system = "\n\n".join([PLAN_SYSTEM, format_catalog(profiles), format_tool_catalog()])
    # volatile: prior context + the question
    parts = []
    if context:
        parts += ["Prior investigation context:", context, ""]
    parts += [f"User question: {question}", "", "Produce the analysis plan."]
    return system, "\n".join(parts)


# --------------------------------------------------------------------------- #
# sql generation
# --------------------------------------------------------------------------- #

SQL_SYSTEM = (
    "You translate a single analytical step into ONE read-only DuckDB SQL query. "
    "Rules: exactly one SELECT statement (a leading WITH is allowed); query only "
    "the given tables; use only columns that exist; never write, ATTACH, COPY, or "
    "call file-reading functions like read_csv/read_parquet. Use DuckDB functions "
    "(e.g. date_trunc, quantile_cont). Round aggregates to 2 decimals. Return the "
    "SQL plus the columns it reads. Do not add a LIMIT unless the step needs top-N.\n\n"
    "JOINs: you MAY join across tables, inferring keys from shared column names. "
    "Choose the join type deliberately — INNER JOIN to keep only matching rows; "
    "LEFT JOIN to keep every row of the base table even without a match; for "
    "'which X have no matching Y' (absence) use LEFT JOIN ... WHERE y.key IS NULL "
    "(an anti-join).\n\n"
    "REUSING PRIOR STEPS: if prior steps from this question are provided and this "
    "step restricts to or builds on a set they produced, you MUST reuse that step "
    "— embed its SQL as a CTE/subquery and INNER JOIN or filter on its keys — "
    "rather than recomputing a different (usually broader) scope. Do not silently "
    "drop a restriction an earlier step established."
)


def format_prior_steps(outcomes: List[StepOutcome]) -> str:
    """Digest of steps already completed in the current question, so a dependent
    step can compose on them (reuse SQL as a subquery, or join on their keys)."""
    if not outcomes:
        return ""
    lines = [
        "Steps already completed in THIS question — reuse their results (embed a "
        "step's SQL as a CTE/subquery, or INNER JOIN / filter on its key columns); "
        "do NOT recompute them with a different scope:"
    ]
    for o in outcomes:
        lines.append(f"\nStep '{o.step.intent}': {o.step.description}")
        if o.sql:
            lines.append(f"  SQL: {o.sql}")
        if o.table is not None:
            lines.append(f"  -> {o.table.row_count} rows; columns: {', '.join(o.table.columns)}")
            for row in o.table.rows_preview[:5]:
                lines.append("    " + " | ".join("" if v is None else str(v) for v in row))
        elif o.tool_result is not None:
            lines.append(f"  -> tool {o.tool_result.tool}: {o.tool_result.summary}")
    return "\n".join(lines)


def build_sql_messages(
    step: PlanStep, profiles: List[DatasetProfile], prior_outcomes: List[StepOutcome] = ()
) -> Tuple[str, str]:
    # stable (cached): stage instruction + table catalog
    system = "\n\n".join([SQL_SYSTEM, format_catalog(profiles)])
    # volatile: prior steps + this step
    parts = []
    prior = format_prior_steps(list(prior_outcomes))
    if prior:
        parts += [prior, ""]
    parts += [f"Analytical step ({step.intent}): {step.description}", "",
              f"Write the DuckDB SQL for step id '{step.id}'."]
    return system, "\n".join(parts)


# --------------------------------------------------------------------------- #
# tool argument generation
# --------------------------------------------------------------------------- #

TOOL_ARGS_SYSTEM = (
    "You produce the input arguments for a named analysis tool, given an "
    "analytical step and the dataset profile. Return ONLY arguments valid for the "
    "tool's schema. Use exact existing column names. Infer concrete values from "
    "the question/step and the profile — e.g. express specific months as 'YYYY-MM' "
    "using the profile's date range; choose sensible defaults for optional args."
)


def build_tool_args_messages(
    step: PlanStep, profiles: List[DatasetProfile], tool: "Tool"
) -> Tuple[str, str]:
    # stable (cached): stage instruction + tool spec + the full table catalog
    system = "\n\n".join([
        TOOL_ARGS_SYSTEM,
        f"Tool: {tool.name} — {tool.description}",
        format_catalog(profiles),
    ])
    user = (
        f"Analytical step ({step.intent}): {step.description}\n\n"
        "Produce the tool arguments. The columns may come from any ONE of the loaded "
        "tables above, but all of a tool's columns must be on the same table."
    )
    return system, user


# --------------------------------------------------------------------------- #
# narrator
# --------------------------------------------------------------------------- #

NARRATE_SYSTEM = (
    "You interpret analysis results for a business audience. Given the question, "
    "the dataset profile, and any result tables, produce: concrete findings "
    "grounded in the numbers (cite specific values), a one-paragraph conclusion, a "
    "confidence level (low/medium/high) reflecting how well the evidence answers "
    "the question, and up to 3 useful follow-up questions. Do not invent numbers "
    "not present in the profile or results. If no result tables are provided, the "
    "question is answerable directly from the dataset profile — answer from it."
)


def _format_outcome(o: StepOutcome) -> str:
    head = f"Step '{o.step.intent}' — {o.step.description}"
    if o.table is not None:
        return f"{head}\n{_format_table(o.table)}"
    if o.tool_result is not None:
        r = o.tool_result
        lines = [f"{head}\nTool {r.tool}: {r.summary}"]
        if r.metrics:
            lines.append("metrics: " + ", ".join(f"{k}={v}" for k, v in r.metrics.items()))
        if r.table is not None and r.table.rows_preview:
            lines.append(_format_table(r.table))
        if r.caveats:
            lines.append("caveats: " + "; ".join(r.caveats))
        return "\n".join(lines)
    return head


INVESTIGATE_SYSTEM = (
    "You are a data analyst answering a question over read-only tables using a "
    "`run_sql` tool (DuckDB). Write ONE read-only SELECT per call — you may use "
    "WITH and JOIN across tables (choose join types deliberately: INNER to keep "
    "matches, LEFT / LEFT…IS NULL for absence). Inspect the returned rows and "
    "iterate as needed. When confident, STOP calling the tool and give a concise, "
    "evidence-backed answer that cites the key numbers. Never fabricate numbers you "
    "did not compute. If the schema alone answers it, answer without any query."
)


def build_investigate_messages(
    question: str, profiles: List[DatasetProfile], context: str = ""
) -> Tuple[str, str]:
    # stable (cached): stage instruction + table catalog
    system = "\n\n".join([INVESTIGATE_SYSTEM, format_catalog(profiles)])
    parts = []
    if context:
        parts += ["Prior investigation context:", context, ""]
    parts.append(f"Question: {question}")
    return system, "\n".join(parts)


SUMMARY_SYSTEM = (
    "You maintain a running summary of a data-analysis investigation. Given the "
    "prior summary and new question→conclusion turns to fold in, produce an "
    "updated, compact summary that preserves the findings, concrete figures, "
    "entities, and decisions a later question might build on. Keep it tight — a "
    "short paragraph or a few bullets; merge duplicates; keep specific numbers and "
    "names; drop narration. Return only the summary text."
)


JUDGE_SYSTEM = (
    "You are a skeptical senior analyst reviewing another analyst's completed data "
    "investigation. Your job is to find what is WRONG or UNDERSUPPORTED, not to praise. "
    "Default to doubt. Read the whole transcript (questions, results, conclusions) and "
    "produce a structured critique:\n"
    "- claims: decompose the MAIN conclusion into its atomic assertions (a sentence like "
    "'X rose, likely because of Y and Z' is THREE claims). Tag each as descriptive / "
    "comparative / causal / forecast and rate it supported / weak / unsupported with why. "
    "Causal and forecast claims deserve the most scrutiny.\n"
    "- assumptions: implicit choices the analyst made that could be wrong (definitions, "
    "proxies, filters, date handling).\n"
    "- weak_conclusions: specific claims that the evidence does not actually support, each "
    "with why it is weak.\n"
    "- missing_evidence: what was never gathered that would confirm or refute the main claim.\n"
    "- untested_alternatives: rival explanations that were never tested, each with how you'd "
    "test it (a concrete query or comparison).\n"
    "- simpler_explanation: if a more parsimonious story could fit the same data, state it; "
    "else null.\n"
    "- confidence_assessment: is the stated confidence earned? Should it be higher or lower, "
    "and why.\n"
    "- next_query: the single most valuable next question to run, and why it matters most.\n"
    "Be concrete and reference the actual findings. Do NOT invent node ids, table names, or "
    "numbers that aren't in the transcript. If the investigation is genuinely sound, say so "
    "briefly in 'overall' and keep the lists short — but still surface the weakest point."
)


def build_review_messages(transcript: str) -> Tuple[str, str]:
    user = ("Here is the full investigation transcript to review:\n\n"
            + transcript +
            "\n\nProduce your structured critique. Lead with the single weakest point.")
    return JUDGE_SYSTEM, user


def build_summary_messages(prior_summary: str, turns: List[Tuple[str, str]]) -> Tuple[str, str]:
    lines = []
    if prior_summary:
        lines += ["Prior summary:", prior_summary, ""]
    lines.append("New turns to fold in (question → conclusion):")
    for question, conclusion in turns:
        lines.append(f"- Q: {question}\n  A: {conclusion}")
    lines.append("\nProduce the updated running summary.")
    return SUMMARY_SYSTEM, "\n".join(lines)


def build_narrate_messages(
    question: str,
    profiles: List[DatasetProfile],
    outcomes: List[StepOutcome],
    context: str = "",
) -> Tuple[str, str]:
    # stable (cached): stage instruction + table catalog
    system = "\n\n".join([NARRATE_SYSTEM, format_catalog(profiles)])
    # volatile: prior context + this question's results
    body = [f"Question: {question}", ""]
    if context:
        body += ["Prior investigation context:", context, ""]
    if outcomes:
        body.append("Results:")
        for o in outcomes:
            body.append("\n" + _format_outcome(o))
        body.append("\nInterpret these results.")
    else:
        body.append(
            "No query results — this question is answerable directly from the "
            "dataset profile provided. Answer it."
        )
    return system, "\n".join(body)
