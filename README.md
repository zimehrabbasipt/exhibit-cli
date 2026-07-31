# Exhibit

An AI-powered data-analysis CLI that treats analysis as a **persistent,
resumable investigation** rather than a one-off chat or SQL session.

You load a CSV/Parquet file and ask a broad question ("Why did revenue decline
in June?"). Exhibit profiles the data, plans analytical steps, generates and
**validates read-only** DuckDB SQL, executes it, interprets the results, and
suggests follow-ups — saving **every step as a typed node** in an append-only
log you can close, reopen, inspect, and re-run.

## Why an investigation, not a chat

An investigation is an append-only sequence of typed nodes, each linked to a
parent so the history forms a tree (branching comes for free):

```
dataset_profile
user_question
  └─ plan
       ├─ sql_query ─ table_result
       ├─ interpretation
       └─ conclusion ─ follow_up*
```

Nothing is a throwaway chat message: conclusions cite the table nodes they rest
on, SQL is stored and replayable, and the whole thing exports to Markdown — or to a
**self-contained HTML viewer** (`export --html`): conclusion cards, evidence chips that
link to collapsible evidence-chain sections, inlined chart images, and a judge-caveats
panel — rendered entirely from the persisted graph with **no LLM calls**. This
is also the substrate for the longer-term vision — an "AI notebook" whose cells
are investigation steps, rendering the same underlying graph.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/make_sample.py     # writes sample_data/{sales,products,customers}.csv
```

## Use

```bash
exhibit start sample_data/sales.csv     # load, profile, enter the investigation
# multiple files → multiple tables (Claude can JOIN across them):
exhibit start sample_data/sales.csv sample_data/products.csv sample_data/customers.csv
# or snapshot selected tables from a live Postgres (read-only) into a local copy:
exhibit start postgresql://user@host:5432/db --tables orders,public.customers
exhibit list                            # list saved investigations
exhibit open <id>                       # resume one
exhibit export <id> -o report.md        # export a Markdown report
exhibit export <id> --html -o report.html   # self-contained HTML viewer (no LLM; charts inlined)
```

### Data sources — files or a live Postgres

A source is one or more local **CSV/Parquet** files (or a folder), **or** a **Postgres
DSN**. For Postgres, Exhibit snapshots the tables you name into the per-investigation
DuckDB — the automated version of "export to Parquet, point Exhibit at it," so the
read-only/reproducible model is unchanged (after import, every query still runs against
local tables with external access off).

```bash
exhibit start postgresql://user@host:5432/db                    # lists tables, then asks you to pick
exhibit start postgresql://user@host:5432/db --tables orders,public.customers
exhibit start postgresql://user@host:5432/db --tables events --max-rows 500000
```

- **Table selection is required** — it never imports the whole database. Run without
  `--tables` to see the catalog, then pick.
- **Row counts are shown for consent** before the copy, and `--max-rows` (default 1,000,000)
  caps each table with a loud truncation warning.
- The snapshot's **provenance is recorded** — redacted DSN, timestamp, and source row count
  go in the profile and render in the viewer header (`snapshot of …:5432/db · Jul 31 14:02 ·
  1.2M rows`), so staleness is visible, not hidden.
- Needs the DuckDB `postgres` extension (auto-installed) and network. **Use a read-only DB
  role** — Exhibit only ever reads, but belt and suspenders.

### Choosing the LLM backend

The engine depends only on an `LLMClient` protocol, so the backend is swappable:

```bash
exhibit start data.csv --llm mock        # deterministic, offline, no API key
exhibit start data.csv --llm anthropic   # Claude (claude-opus-4-8) via the anthropic SDK
exhibit start data.csv --llm auto        # Claude if anthropic SDK + ANTHROPIC_API_KEY, else mock (default)
```

`--llm anthropic` needs `pip install "exhibit-cli[anthropic]"` and Anthropic credentials
(`ANTHROPIC_API_KEY` or an `ant auth login` profile). With Claude, a broad question
produces a real multi-step plan (confirm trend → decompose by dimension → volume-vs-value),
generates DuckDB SQL per step, and writes evidence-backed conclusions.

Inside an investigation, plain text is an analytical question. Slash commands:

| command | action |
| --- | --- |
| `/schema` | show all loaded table profiles |
| `/add <file>` | load another CSV/Parquet as a new table (enables joins) |
| `/plan` | show the most recent plan |
| `/history` | list all nodes |
| `/show <id>` | render a node by short id |
| `/sql <id>` | print a query's SQL |
| `/rerun <id>` | re-run a stored SQL query — or a whole plan's task list — deterministically |
| `/chart <id> <type> x=.. [y=..]` | chart a result table (line/bar/scatter/histogram) |
| `/artifacts` | list saved files |
| `/quick <text>` | fast single-loop answer (fewer calls; no tools/follow-ups) |
| `/branch <id> <text>` | fork a question off an earlier node — a real sibling branch, not the latest thread (context scoped to that node's ancestry) |
| `/judge [id]` | adversarial review of the whole investigation (or a branch, if `<id>` given): assumptions, weak conclusions, missing evidence, untested alternatives, a simpler-explanation check, and the highest-value next query |
| `/judge --branches <a> <b>` | compare two branches head-to-head — conflicts, **definition drift** (same metric operationalized differently), which explanation the evidence better supports, shared-evidence circularity |
| `/judge --descendants <id>` / `--unresolved` | review a subtree, or focus on still-open hypotheses (which remain live + the single test that resolves the most) |
| `/graph` | deterministic graph checks (shared evidence, orphan conclusions, untested hypotheses, **metric drift**) + hypotheses with their edge-derived status |
| `/metric <name> = <sql> [\| desc]` / `/metrics` | define / list reusable named metrics — a **semantic layer**: the definition is fed into later planning & SQL so it's reused, not re-derived (prevents definition drift) |
| `/export [html]` | write a report — Markdown, or a self-contained HTML viewer (`/export html`) |
| `/cost` | token usage + cost for the session, incl. prompt-cache savings |
| `exit` | leave |

Plain questions run the **full investigation** in **batch mode**: one planner
call emits an ordered task list with each step's *executable command inline*
(SQL, or a tool + JSON args), the engine runs them all deterministically
(guarded, in DuckDB / the stats tools), then a single narrate call produces the
evidence-backed conclusion + follow-ups. That's **2 LLM calls regardless of step
count** (vs one per step before). The task list is saved in the plan node, so
`/rerun <plan-id>` replays the whole thing. `/quick <text>` opts into a **fast
path** — a single agentic `run_sql` loop — that skips the plan, stats tools, and
follow-ups.

### Self-review: the investigation judge

Exhibit reviews its own work at two levels:

- A **deterministic linter** runs after *every* conclusion (no LLM, no extra cost). It
  flags structural weaknesses — a conclusion grounded in no result, steps that errored,
  causal language with no statistical test, high confidence on thin evidence, or a result
  that hit the row cap and may be truncated — and surfaces only the warnings (`⚠ self-check`).
- An **LLM judge** you summon at checkpoints with **`/judge`** (whole investigation) or
  **`/judge <node-id>`** (a branch's ancestry). It's an adversary pointed inward: it returns
  the implicit **assumptions**, **weakly-supported conclusions**, **missing evidence**,
  **untested alternative hypotheses** (each with how to test it), a **simpler-explanation**
  check, a **confidence assessment**, and the single **highest-value next query** — saved as a
  `critique` node in the graph and included in `/export`. Its untested alternatives are
  **materialized as first-class `hypothesis` nodes**, so a later review can see which
  alternatives are still open.

The judge is **graph-aware**. Alongside the conversational tree, Exhibit keeps a typed **edge
DAG** (`supports` / `tests` / `alternative_to` / … with a `created_by` provenance stamp), and
the deterministic **`/graph`** checks reason over it — flagging *evidence shared across divergent
branches* (a comparison that's partly circular), *orphaned conclusions*, and *untested
hypotheses*. A hypothesis's status (`proposed` → `supported` / `weakened` / `rejected`) is
**derived from its edges**, never a stored field.

The judge decomposes each conclusion into **atomic claims** — a sentence like "revenue rose,
*because of* price and *because of* mix" is three claims (descriptive, causal, causal), each
rated `supported`/`weak`/`unsupported` on its own, so a solid finding and a speculative
because-clause in the same paragraph are scored separately.

Analytical structure is captured as typed edges: the planner declares which steps **build on**
which (`depends_on`), and a **semantic metrics layer** (`/metric`) lets you define a quantity
like `squad_value` once so every later query reuses the exact definition — with a deterministic
**metric-drift** check when the same name is defined two ways.

Instead of a flat transcript, `/judge` now receives a **structured digest** — a GRAPH STRUCTURE
section (each conclusion with the evidence that supports it, plus the semantic relationships
between nodes), the deterministic GRAPH WARNINGS, and the chronological transcript only as a
secondary view. That lets it reason over relationships prose can't show. Subgraph selectors focus
the review: **`--branches`** compares two branches head-to-head (the pass that catches
same-metric-defined-differently *definition drift*), **`--descendants`** reviews a subtree, and
**`--unresolved`** targets the open hypotheses. See `ROADMAP.md`.

### Branching off an earlier turn

By default each question threads under the **latest** conclusion — a straight
line. To explore an alternative direction from an *earlier* point without
disturbing the main line, branch off that node explicitly:

```text
exhibit> /history                 # find the node to fork from
...
[41] conclusion · 1c0d268f   total_revenue bottomed out at 931.09 in June ...
[65] conclusion · b0800c29   revenue is concentrated in the West region ...

exhibit> /branch 1c0d268f now break that same June dip down by product category
Branching off 1c0d268f (conclusion)
...
```

The new question's parent becomes the node you named (not the latest turn), so
the investigation graph gets a **real sibling branch**. Its context is scoped to
that node's **ancestry** — the chain of parents up to the root — so the branch
reasons only from its own lineage and is blind to what you explored on other
branches. That's what keeps two forks genuinely independent.

- The id is any unambiguous prefix of the short id shown in `/history` (e.g.
  `1c0d`), the same resolver `/show` and `/sql` use.
- You'll usually branch off a `conclusion` (the natural "continue from here"
  point), but any node works.
- The tree structure is preserved in the graph and rendered in `/export`.

## Analysis tools

Deterministic, exact tools the planner selects by name (each runs in DuckDB /
scipy / numpy, never LLM arithmetic):

- **Summary & detection:** `describe`, `outliers`, `fit_distribution` (Poisson/Gaussian)
- **Change decomposition:** `decompose_contribution`, `volume_vs_rate`
- **Hypothesis tests & correlation:** `t_test`, `mann_whitney`, `chi_square`,
  `correlation` (Pearson/Spearman + p-value), `correlation_matrix`
- **Time series:** `moving_average`, `growth_rates` (MoM/YoY), `trend` (slope/R²/change-point)
- **Modeling:** `linear_fit` (OLS + R²), `logistic_fit` (IRLS + pseudo-R²)
- **Customer analytics:** `cohort_retention`, `funnel`, `rfm`

Run any of them by hand with `/tool <name> k=v …`, or let the planner pick.

## Charts

Each investigation **auto-charts its headline result** when it's chartable
(a line for time series, a bar for categoricals) — rendered as a polished PNG
artifact styled from a validated data-viz design-system palette
(clean surface, hairline grid, muted axes, value labels), with a terminal
preview. Charts are `chart` nodes in the graph and are **embedded as images in
the Markdown export**. Chart anything by hand with
`/chart <table-id> <line|bar|scatter|histogram> x=<col> [y=<col>]`.

## Safety: read-only by construction

Generated SQL cannot mutate or exfiltrate data. Two layers:

1. The user's data is materialized into DuckDB once at load time; every query
   connection is opened **read-only with `enable_external_access=false`**.
2. A static guard (`exhibit/engine/sqlguard.py`) parses each query with sqlglot
   and rejects anything but a single `SELECT`/CTE, blocks file-reading table
   functions, and enforces a `LIMIT`.

## Architecture

The engine depends only on an `LLMClient` protocol. A deterministic `MockLLM`
lets the entire pipeline run offline and be unit-tested; the `AnthropicLLM`
adapter (Claude, structured outputs via `messages.parse`) is a drop-in swap.

```
exhibit/
  models.py            # Pydantic contracts + typed node payloads (discriminated union)
  config.py            # ~/.exhibit layout, limits, prompt version
  store/               # SQLite (investigations + append-only nodes) + artifact paths
  data/                # DuckDB read-only loader + deterministic profiler
  llm/                 # LLMClient protocol, MockLLM, AnthropicLLM, prompts, factory
  tools/               # 18 deterministic analysis tools (see below), auto-selected by the planner
  engine/              # planner → sqlgen → sqlguard → executor → narrator → orchestrator
  render.py            # Rich terminal rendering
  repl.py / cli.py     # interactive loop + Typer commands
  export.py            # Markdown export
```

State lives under `~/.exhibit` (override with `EXHIBIT_HOME`): SQLite metadata,
per-investigation DuckDB file, and artifacts.

## Status

Working: CSV/Parquet loading (one or more files → one table each, joinable),
`/add` to bring in tables mid-investigation, deterministic profiling, persistent resumable
investigations, **`/branch` to fork a question off any earlier node** (a real sibling
branch in the graph, with context scoped to that node's ancestry so forks stay
independent), **composable steps** (a later step reuses an earlier step's SQL as
a CTE/subquery, so "analyze only the set found in step 1" stays correctly scoped)
with deliberate INNER/LEFT/anti join choice, plan → (read-only SQL **or**
LLM-selected analysis tool) → table/
tool result → evidence-backed conclusion → follow-ups, deterministic analysis
tools (auto-selected by Claude, or manual via `/tool`), zero-step direct answers
from the profile, **cumulative follow-ups** (each question sees a compact digest
of prior Q&A + recent results and threads under the previous conclusion),
Markdown export, and both `mock` and `anthropic` backends.

With Claude, a broad question yields a mixed plan — e.g. SQL for the monthly
trend, then `decompose_contribution` and `volume_vs_rate` (arguments inferred
from the question) — synthesized into one grounded conclusion; a follow-up
("anything interesting about their locations?") builds on the rows already found
instead of recomputing them.

Prompts are split into a stable, **prompt-cached** system prefix (stage
instruction + dataset catalog) and a volatile user message, so repeated calls in
an investigation read the catalog from cache (~0.1×) instead of re-paying for it.

**Roadmap:** rolling investigation summary (true compaction of old turns) ·
tools that run on a prior step's result · charts (PNG + terminal preview).

## Tests

```bash
pytest        # guard, profiler, end-to-end pipeline + resume
```

## License

**Source-available** under the [Business Source License 1.1](LICENSE) — the source is
open to read, and you may use Exhibit freely in production, *except* to offer a commercial
product or hosted service whose primary purpose is automated data analysis substantially
similar to Exhibit. On the Change Date (2030-07-31) each released version converts
automatically to **Apache-2.0**. (Not an OSI open-source license.)
