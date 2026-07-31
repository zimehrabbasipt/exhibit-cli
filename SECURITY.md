# Security Policy

Exhibit is **read-only by construction**: user data is materialized once into a
per-investigation DuckDB file, and every query connection is opened
`read_only=True` with `enable_external_access=false`. Generated SQL is additionally
parsed by a static guard (`exhibit/engine/sqlguard.py`) that permits only a single
`SELECT`/`WITH`, blocks writes/DDL/`ATTACH`/`COPY`/`PRAGMA` and file-reading table
functions, and enforces a `LIMIT`. See `THREAT_MODEL.md` for what this does and does
not guarantee.

## Reporting a vulnerability

If you find a way to make Exhibit write, mutate, exfiltrate, or read outside the
loaded tables — a **guard bypass** — please report it privately:

- Email **zimehrabbasigh@gmail.com** with steps to reproduce, or
- Open a [GitHub security advisory](https://github.com/ziwalker/exhibit-cli/security/advisories/new).

Please do not open a public issue for a suspected bypass until it's fixed. We aim to
acknowledge within a few days.
