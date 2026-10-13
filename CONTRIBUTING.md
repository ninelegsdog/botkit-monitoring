# Contributing to botkit-monitoring

Thanks for considering it. This project is part of a family of production bots, so
a change here is a change to something running — a conversation before a large
patch will save everyone time.

## Getting set up

Requires **Python ** and
[`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ninelegsdog/botkit-monitoring.git
cd botkit-monitoring
cp .env.example .env
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

Copy `.env` and fill it in before running anything against Telegram.

## Before opening a pull request

```bash
uv sync --frozen --all-extras
pre-commit run --all-files
uv run --frozen pytest -q
```

All three must pass. CI (ci.yml, e2e-smoke.yml, secret-audit.yml) additionally checks that `uv.lock` still matches
`pyproject.toml`, runs gitleaks over the history, scans the filesystem with Trivy,
publishes an SBOM, and validates the k6 load script.

If you touched test infrastructure, say in the description which failure modes you
verified — for example, that you confirmed a new test can still fail.

## Pull request rules

- One concern per pull request.
- Explain **why**, not just what. The diff already shows what.
- Use the pull-request template; it asks for the verification that makes a change
  reviewable.
- If behaviour changes, update `CHANGELOG.md` under `[Unreleased]`.
- Conventional Commits for the subject: `feat:`, `fix:`, `docs:`, `test:`,
  `chore:`, `security:`, `refactor:`.

## Writing tests

Read [AGENTS.md](AGENTS.md) first. The short version:

- Tests run offline by default. A test that needs the network or a Telegram
  account is marked `req` and stays skipped in CI.
- No `time.sleep` — wait on a real condition.
- A new test must be able to fail. Add it, then break the thing it guards and
  confirm it goes red. A test that has never failed is unverified.

See [README-testing.md](README-testing.md) for markers, commands and the opt-in
E2E suite.

## AI-assisted contributions

Fine, and encouraged — this is an AI-native project. Two expectations:

1. Say which parts were agent-generated. Reviewers weigh unreviewed generated code
   accordingly.
2. The agent must have actually run the commands above, and the output belongs in
   the description. "The agent says it passes" is not evidence.

## Reporting bugs

Use the bug-report issue form. For a suggested change, use the feature-request
form; it asks how "done" would be verified, because a change that cannot be
observed failing is not finished.

## Security

Do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).

## Code of conduct

Be straightforward and assume good faith. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
