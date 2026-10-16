# AGENTS.md

Rules for coding agents working in this repository. Read before changing code.

## What this project is

botkit-monitoring: the observability stack for the nine BotKit bots — Prometheus,
Alertmanager, Grafana, Loki, promtail, Tempo, the OpenTelemetry Collector and a reverse
proxy. It is a compose-only stack of vendor images. Nothing here is built, and nothing
here is a Telegram bot.

What does run here is `ops/`: the shell and Python that execute on the host — the e2e
smoke checks, the backup and restore verification, the TLS and drift checks, the rollout
tooling. A monitoring stack whose own checks rot is silent: a Prometheus rule that fails
to parse is an alert that never fires, and a drift check that stops running is an outage
that reports itself as healthy.

## Hard rules

These are enforced in CI (`ci.yml`, `e2e-smoke.yml`, `secret-audit.yml`). Do not work around them.

1. **Never commit secrets.** Use `.env`; keep `.env.example` free of real values.
   gitleaks scans the history, and a leaked token is an incident, not a revert.
2. **Never leave a test that needs the network unmarked.** A test that reaches out to a
   host is marked `req` and stays skipped in CI. An unmarked network test is a flaky red
   on someone else's runner.
3. **Never sleep.** No `time.sleep` in a test. Wait on a real condition, so the test
   retries instead of guessing.
4. **Never weaken a gate to make a build green.** gitleaks, `promtool check`,
   `docker compose config`, `shellcheck` and the `ops/e2e` suite are load-bearing. Fix
   the thing they are complaining about.
5. **Never commit a rendered or evidence artifact.** `.env`, `*.session`, coverage
   output and log files are output, not source.

## Before you finish

Run and show the output of the same commands `ci.yml` runs:

```bash
ruff check --output-format=concise .
(cd ops/e2e && PYTHONPATH=. python -m pytest -q)
python3 scripts/check-repo-contract.py . scripts/repo-contract.json
```

CI runs more than these where the change touches them: `shellcheck` and `bash -n` over
`ops/**/*.sh`, `promtool check config`/`check rules`/`check metrics`, `docker compose
config`, the Grafana dashboard JSON parse and `yamllint`. Run the ones your diff can
affect.

Do not report a task as done on the basis of a green build alone. Confirm the
observable behaviour: the check ran, and it failed when the thing it guards was broken.

**Check the exit code, not a line of output.** Grepping for the string you expect
is a claim that you looked, not a check: it passes while the build is green and
stays silent when the build is red.

## Tests must be able to fail

Every new test gets a negative test: break the thing it guards and confirm it goes red.
A test that has never failed is unverified. The suite runs offline by default, so this
must be demonstrable without reaching a live host.

## Structure

`ops/` is the host-side tooling and the only Python/shell that runs: `e2e/` (smoke and
scenarios), `backup/` (restic verification and restore drill), `drift/`, `health/`,
`rollout/`, `tls/`, `host/`, `redis/` and `lib/` (the shared `fleet.sh`). The component
directories — `prometheus/`, `alertmanager/`, `grafana/`, `loki/`, `tempo/`, `otelcol/`,
`promtail/`, `reverse-proxy/` — are configuration for vendor images. Do not add an
application module: there is no application here.

## Style

- Python 3.10+, no web framework.
- Imports ordered: stdlib, then third-party, then local.
- Type hints on public functions.
- Line length ≤120. PEP 8, `ruff check .`.
- Shell: `set -euo pipefail`, quoted expansions, clean under `shellcheck --severity=warning`.
- Conventional Commits: `feat`, `fix`, `docs`, `test`, `chore`, `security`, `refactor`.
- One concern per commit. A commit that fixes a bug and reformats a file is two commits.

## Parallel work

Several agents may work on this repository at once, but not in one working tree.
`CHANGELOG.md`, `AGENTS.md` and `pyproject.toml` are touched by most changes; two
agents in one tree collide on the first commit that touches documentation.

Give each agent its own worktree and branch. The integrator's verification is
serial and cannot be delegated — that is the real ceiling on how far this
repository parallelises.

## Docker

`user 1001:1001`, `read_only` with tmpfs for writable paths, `cap_drop: ALL`,
`no-new-privileges`. If you need a new capability, say why in the pull request
instead of adding it quietly.
