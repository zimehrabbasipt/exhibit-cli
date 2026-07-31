# Contributing to Exhibit

Thanks for your interest.

## License & sign-off (DCO)

Exhibit is source-available under the **Business Source License 1.1** (see
[`LICENSE`](LICENSE)); contributions are accepted under that same license. We use
the [Developer Certificate of Origin](https://developercertificate.org/): by
signing off on your commits you certify you wrote the patch, or otherwise have the
right to submit it under the project's license.

Sign off every commit:

```bash
git commit -s -m "…"      # adds: Signed-off-by: Your Name <you@example.com>
```

PRs with unsigned-off commits will be asked to amend.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Include tests for behavior changes, keep the suite green, and match the style of
the surrounding code.

## Security

Please don't open a public issue for a suspected guard bypass — see
[`SECURITY.md`](SECURITY.md).
