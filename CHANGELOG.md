# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Added

- `test_unit_sets_a_private_umask_for_what_it_writes` pins `UMask=0077` in the E2E
  unit. The session is an account credential and the status markers are the runner's
  only other output; without the directive both are created with the host umask, and
  a manual `runuser ... python -m e2e.run_e2e` — the command an operator reaches for
  when verifying — bypasses the unit and leaves one world-readable `0644` marker
  among the `0600` ones.
- The repository presents itself as it is. The README became a front page — pitch,
  badges, a table of contents and user-facing sections — and issue forms plus a
  pull-request template now ask for the evidence this project acts on: what was
  run, what was expected, and how "done" would be able to fail.
- Governance layer brought to the family standard: `CONTRIBUTING.md`,
  `SECURITY.md`, `CODE_OF_CONDUCT.md`, `AGENTS.md`, `.editorconfig`,
  `.gitattributes` and `CODEOWNERS`.
- `scripts/check-repo-contract.py` checks this repository against the shared
  contract, and `scripts/check-repo-contract_test.py` proves that checker can
  fail. A gate that has never gone red is an assumption, not a gate.
- Long-form documentation that already existed but was not linked is now linked from the front page: `docs/adr/0001-offsite-backup-restic-over-git.md`, `docs/adr/0002-rpo-rto-backup-immutability.md`.

### Fixed

- The E2E message budget was three different numbers at once — 76 a day in the
  installed timer, 96 in the repository copy and 36 in a test docstring — and none
  of them counted button presses, which reach Telegram as outbound calls too. The
  number is now derived from `scenarios.yml` and `OnCalendar` (25 steps = 23 sends
  + 2 presses, 4 runs, plus a run summary = 104 a day) and asserted against the
  timer comment, so prose cannot drift from the scenarios again.
- The E2E runner's Alertmanager alert now carries its own `endsAt`. Without it
  Alertmanager dropped the alert after its `resolve_timeout`, so "the bot is
  still broken" and "the runner stopped reaching Alertmanager" were the same
  event on the receiving side. The hold window now outlasts the gap to the next
  run (6h timer plus jitter), and only the runner lets an alert close.
- `README-testing.md` was unreachable from the README in every bot of the family
  except one. A document nobody can reach is a document nobody reads; it is linked
  from the front page now.

### Changed

- Line endings are pinned to LF by `.gitattributes`, so a CRLF contributor no
  longer turns every touched line into a diff.
- `AGENTS.md` and the pull-request template describe this repository instead of a
  bot. They had been copied from the family's `telegram-bot` template: the project
  called itself "a production Telegram bot", the finish commands were `uv run
  --frozen pytest` and `pre-commit run --all-files` — neither runnable here, since
  this repository has no lockfile and no pre-commit config — and the rules pointed
  at `src/delivery/webhook_api.py` and aiogram. The finish commands are now the
  three `ci.yml` actually runs (`ruff`, the `ops/e2e` suite, the repo contract),
  and the rules name the gates that exist here.

## [] — 2026-10-13

Project running in production. See the git history for the work preceding this
entry; the changelog starts here because the repository had none.
