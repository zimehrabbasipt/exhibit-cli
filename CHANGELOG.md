# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-07-31

Initial public release.

### Added
- Investigation engine: append-only typed-node graph with branching and a rolling summary.
- Read-only DuckDB loader + static SQL guard (single `SELECT`; blocks writes/`ATTACH`/`COPY`/file access).
- 18 deterministic analysis tools; a tool runs on whichever loaded table holds its columns.
- Planner/narrator with an Anthropic backend (prompt caching + model routing) and a deterministic mock backend.
- Self-review: a deterministic linter after every conclusion, plus an on-demand LLM judge.
- Typed edge DAG, first-class hypotheses (status derived from edges), and deterministic graph checks.
- Semantic metrics layer (`/metric`) — define a quantity once and reuse it.
- Postgres read-only snapshot import (required table selection, row cap, recorded provenance).
- Markdown and self-contained HTML-viewer exports.
