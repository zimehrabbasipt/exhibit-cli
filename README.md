# Exhibit

An AI data-analysis CLI that treats analysis as a **persistent, resumable
investigation** — an append-only graph of typed steps (question → plan →
read-only SQL → result → evidence-backed conclusion), not a chat transcript.
Load a CSV/Parquet file (or snapshot a Postgres table), ask in plain English,
and get an inspectable, replayable, exportable investigation.

**Read-only by construction.** Data is loaded once into a per-investigation
DuckDB opened read-only with external access disabled; every generated query is
parsed by a static guard that permits a single `SELECT` and blocks writes,
`ATTACH`, `COPY`, and file access. See [`THREAT_MODEL.md`](THREAT_MODEL.md).

## Install

```bash
pip install exhibit-cli
export ANTHROPIC_API_KEY=...        # or run offline with --llm mock
```

## Use

```bash
exhibit start data.csv                                   # one file
exhibit start orders.csv customers.csv                   # several (joins across them)
exhibit start postgresql://user@host/db --tables orders  # live Postgres (read-only snapshot)
```

Then ask questions in plain English. Inside an investigation:

- `/judge` — adversarial self-review (weak claims, missing evidence, untested alternatives)
- `/branch <id> <question>` — fork a question off an earlier step
- `/export [html]` — Markdown, or a self-contained HTML report
- `/help` — everything else

Resume anytime with `exhibit open <id>`.

## License

Source-available under the [Business Source License 1.1](LICENSE): read the
source and use it freely in production, but don't offer a competing commercial
or hosted data-analysis product. Converts to Apache-2.0 on 2030-07-31. Not an
OSI open-source license.
