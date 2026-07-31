# Threat model

Exhibit runs LLM-generated SQL against your data. This documents what it guarantees,
what it doesn't, and where the sharp edges are — honestly.

## What the guards enforce

1. **Read-only DuckDB connection.** Query connections use `read_only=True` and
   `enable_external_access=false`, so a query cannot write files, open network
   connections, install extensions, or `ATTACH` other databases — enforced by the
   engine, not by trusting the model.
2. **Static SQL guard** (`sqlguard.py`): parses each query with sqlglot and rejects
   anything but one `SELECT`/`WITH`; blocks `INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/
   COPY/INSTALL/LOAD/PRAGMA/SET/CALL` and file-reading functions (`read_csv`,
   `read_parquet`, `glob`, …); auto-adds a `LIMIT`.
3. **Deterministic tools** compute statistics in scipy/numpy — never LLM arithmetic —
   so results are exact and reproducible, not model-hallucinated.

## What it does NOT protect against

- **Prompt injection via data.** Column values/names are shown to the model as
  context. Malicious content in your data could influence the model's *analysis or
  narration*. It cannot escape the read-only guards above, but treat conclusions drawn
  over untrusted data with the same skepticism you'd apply to any LLM output.
- **The Postgres snapshot path** opens one connection with external access enabled to
  copy selected tables in, then reverts to the read-only model for all querying. Use a
  **read-only database role** as defence in depth.
- **Cost/prompt exposure.** Prompts (including a schema catalog and small row previews)
  are sent to the configured LLM provider. Nothing is sent when using `--llm mock`.

## Reproducibility

Every step is a persisted, replayable node; stored SQL re-runs deterministically
(`/rerun`) with no LLM call. This is the substrate the safety story rests on.
