# AGENTS.md

Rules for coding agents working in this repository. Read before changing code.

## What this project is

botkit-monitoring: a production Telegram bot. It runs in production on webhook
transport, so a change here is a change to something live.

## Hard rules

These are enforced in CI (ci.yml, e2e-smoke.yml, secret-audit.yml). Do not work around them.

1. **Never commit secrets.** Use `.env`; keep `.env.example` free of real values.
   gitleaks scans the history, and a leaked token is an incident, not a revert.
2. **Never leave a test that needs the network unmarked.** A test that requires
   Telegram or an outbound connection is marked `req` and stays skipped in CI. An
   unmarked network test is a flaky red on someone else's runner.
3. **Never leave a floating promise.** A missing `await` on an aiogram call makes
   a test pass while asserting nothing.
4. **Never sleep.** No `time.sleep` in a test. Wait on a real condition, so the
   test retries instead of guessing.
5. **Never weaken a gate to make a build green.** `uv lock --check`, gitleaks,
   Trivy and `pre-commit` are load-bearing. Fix the code they are complaining about.
6. **Never let an unauthenticated event reach the state machine.** The webhook
   secret check is in `src/delivery/webhook_api.py` and is security-critical.
7. **Never pin a dependency by hand in `pyproject.toml` alone.** `uv.lock` is the
   resolved set and CI fails when the two part ways.
8. **Never commit an evidence or run artifact.** `.env`, `*.session` and coverage
   output are output, not source.

## Before you finish

Run and show the output of:

```bash
uv run --frozen pytest -q
pre-commit run --all-files
python3 scripts/check-repo-contract.py . scripts/repo-contract.json
```

Do not report a task as done on the basis of a green build alone. Confirm the
observable behaviour: the test ran, and it failed when the thing it guards was
broken.

**Check the exit code, not a line of output.** Grepping for the string you expect
is a claim that you looked, not a check: it passes while the build is green and
stays silent when the build is red.

## Tests must be able to fail

Every new test gets a negative test: break the thing it guards and confirm it goes
red. A test that has never failed is unverified. The suite runs offline by default,
so this must be demonstrable without a Telegram account.

## Structure

the package — `core/` carries the cross-cutting runtime shared across this family, and
the domain module carries this bot's logic. Do not add a module that belongs in
either without saying so in the pull request.

## Style

- Python , aiogram 3, SQLAlchemy 2 async.
- Imports ordered: stdlib, then third-party, then local.
- Type hints on public functions.
- Line length ≤120. PEP 8, `ruff check .`.
- Conventional Commits: `feat`, `fix`, `docs`, `test`, `chore`, `security`, `refactor`.
- One concern per commit. A commit that fixes a bug and reformats a file is two
  commits.

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
