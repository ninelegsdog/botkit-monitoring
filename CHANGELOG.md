# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Added

- The webhook-check runtime bundle is installed from the canon instead of by hand.
  `ops/e2e/install-webhook-check.sh` writes the four `ops/e2e` scripts and
  `ops/lib/fleet.env` into a root-owned directory (`0700`, config `0600`), atomically,
  and verifies the hashes it wrote; a `--check` run reports `DRIFT` or `MISSING`
  without touching anything. `sync-canon.sh` runs it on the real path, so `SYNC OK`
  again means the runtime equals the canon, and `check_drift.sh` exports
  `botkit_drift_ok{bot="monitoring"}` so the comparison shows up in Grafana, not only
  in the log.

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

- The runtime `botkit-webhook-check.service` executes lives outside git and nothing
  updated it. `sync-canon.sh` asserted the *clone* — the one copy that updates itself —
  while the copy systemd actually runs sits in a root-owned `/root/botkit-webhook-check`
  that had to be maintained by hand. On 16.10 it was two versions behind the canon, and
  its `fleet.env` was missing `ALERTMANAGER_ALERTS_URL`, which is the difference between
  a failed webhook posting an alert and printing "alert not sent" — every gate was green.
  `sync-canon.sh` now installs the bundle from the canon and `check_drift.sh` compares the
  installed copy against it on every run, so the copy outside git has a gate of its own
  for the first time. (P1)
- Every Alertmanager producer now names its own alert lifetime. A payload without
  `endsAt` lets Alertmanager apply its own `resolve_timeout` (5m), while the producers
  re-fire every 15–60 minutes — so a still-broken condition was absent from
  `/api/v2/alerts` most of the time, and the rollout alert logged `alert sent (critical)`
  into an empty Alertmanager. `deploy_rollout.sh`, `check_drift.sh`, `otlp-e2e.sh`,
  `smoke_all.sh` and `check_backups.sh` now each set an `endsAt` longer than their own
  throttle window, and judge delivery by the HTTP status instead of curl's exit code:
  curl exits 0 on the 400 Alertmanager returns for a payload it rejects, so the old
  `curl -s … && log sent` reported refused alerts as delivered, and `check_backups.sh`'s
  `|| true` also wrote the throttle marker, suppressing the retry for six hours. In the
  same class, `webhook_check.py`'s `resolve_alert` posted `startsAt == endsAt`, which
  Alertmanager rejects outright (*"start time must be before end time"*) — so a recovered
  bot's alert never cleared and stayed firing for its whole hold window. A new
  `test_alert_contract.py` reads `send_alert` out of every producer and fails if `endsAt`
  or the HTTP-status check disappears. (P1)
- `botkit-rollout-check.service` now grants its nine bot clones, so the clone-sync
  gate can actually run. `sync_clone` writes the checkout it deploys from (`git pull
  --ff-only` as the clone's owner, plus an ownership pass), but the unit pinned
  `/home/deploy` with `ReadOnlyPaths` — under `ProtectSystem=strict` every real
  rollout then refused with `Read-only file system (os error 30)`, the unit sat
  `FAILED`, and the fleet could not update. It stayed invisible for a day because
  `verify_rollout.sh`'s dry run never writes the clone and the readwrite contract
  test only inspects write targets it can see as string literals — the clone path is
  computed from `$DEPLOY_DIR`, so no test had it as an input. `/home/deploy` stays
  read-only above the clones, so the canon the unit runs from is still protected; a
  new contract test derives the clones from `check_updates.sh`'s `BOTS` and the unit's
  grants and fails if they disagree. Found by running the unit, not reading it. (P0)
- `check_drift.sh` fetched each bot clone as **root**, and the clones belong to
  `deploy:deploy`. A root-run `git fetch` writes root-owned loose objects (mode 600)
  into the object store, after which deploy-user git cannot even read it — *"insufficient
  permission for adding an object to repository database"*. On 15.10 that left all nine
  clones un-fetchable, which is why the new clone-sync gate in `deploy_rollout.sh` could
  never pass: the gate's own `git pull` runs as `deploy` and failed on the very objects the
  drift check had written. The fetch now drops to the clone's owner (guarded on being root,
  so the offline contract tests still run it directly). (deferred-work D12)
- `deploy_rollout.sh` syncs the bot's checkout before it touches the container, and
  refuses the rollout unless `HEAD == origin/main` afterwards. A rollout updates the
  *image*, but the production clone is a separate artifact, and nothing joined the two:
  on 13.10 every container ran the current image while all nine clones sat behind their
  main, so the drift check went red for the whole fleet (`sync-canon.sh` syncs only the
  monitoring clone). The gate re-owns the checkout (never the container's `data/` and
  `backups/` mounts, which must stay on the runtime uid) and runs `git pull --ff-only`
  as the clone's owner — a root-run fetch poisons the clone for the next deploy-user git
  — and sits *before* the
  "already on target image" skip, because in the drift case the image already equals the
  target and the skip would return early over the stale checkout. (deferred-work D12)
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
