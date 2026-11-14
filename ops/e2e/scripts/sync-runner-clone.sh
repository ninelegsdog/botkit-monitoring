#!/usr/bin/env bash
# sync-runner-clone.sh — bring the E2E runner clone to origin/main before it is read.
#
# Why this exists: /opt/botkit-e2e is a git clone, and host-setup.sh creates it once and
# never touches it again. So the runner executes whatever commit was cloned on the day the
# host was provisioned. On 14.11 that was b10d1e8 from 15.10 — 24 commits behind
# origin/main, including every E2E change merged since. The two chronic failures
# (BotNotAnswering on membership, UnexpectedReply on reminder) had already been corrected
# in canon; the runner could not see the correction, so Telegram was paged every six hours
# for a defect that no longer existed in the repository. A clone keeps itself current only
# if something runs git.
#
# Why `reset --hard` and not `pull`: the clone is a read-only artifact of canon. A local
# edit in it is a mistake to discard, and a merge conflict at 00:17 would take the runner
# down instead of reporting on the bots.
#
# Why no `git clean -xdf`: the venv lives inside the clone and is untracked. Cleaning would
# delete the interpreter and the next run would fail before it touched a bot.
#
# Why root: the clone is root-owned 0750 so the unprivileged runner cannot rewrite the code
# it is about to execute. Updating it therefore needs root — which is also why this is a
# separate unit and not an ExecStartPre of botkit-e2e.service: User= applies to ExecStartPre
# as well, so the runner would have to own its own code to update it.
#
# Environment: CLONE (default /opt/botkit-e2e), RUNNER_USER (botkit-e2e), BRANCH (main),
# GIT (git). The overrides exist so the tests can run this against a throwaway repository.
set -euo pipefail

CLONE=${CLONE:-/opt/botkit-e2e}
RUNNER_USER=${RUNNER_USER:-botkit-e2e}
BRANCH=${BRANCH:-main}
GIT=${GIT:-git}

die() { echo "FATAL: $*" >&2; exit 1; }

[ -d "$CLONE/.git" ] || die "$CLONE is not a git clone; nothing to sync (see ops/e2e/scripts/host-setup.sh)"
command -v "$GIT" >/dev/null 2>&1 || die "git is not installed"

before=$($GIT -C "$CLONE" rev-parse --short HEAD) || die "cannot read HEAD of $CLONE"
dirty=$($GIT -C "$CLONE" status --porcelain | wc -l)
reqs_before=$(sha256sum "$CLONE/ops/e2e/requirements.txt" 2>/dev/null | awk '{print $1}')

# No --quiet: a fetch that fails silently is how a rollout reports success while changing
# nothing. The sync unit runs before the runner, so a failure here must stop the run rather
# than let the runner report on code that is not canon.
$GIT -C "$CLONE" fetch --prune origin "$BRANCH" || die "git fetch failed; the clone is still at $before"

target=$($GIT -C "$CLONE" rev-parse --short "origin/$BRANCH") || die "cannot resolve origin/$BRANCH"
behind=$($GIT -C "$CLONE" rev-list --count "HEAD..origin/$BRANCH")

$GIT -C "$CLONE" reset --hard "origin/$BRANCH" >/dev/null || die "reset --hard origin/$BRANCH failed"

after=$($GIT -C "$CLONE" rev-parse --short HEAD)
[ "$after" = "$target" ] || die "HEAD=$after after the reset, origin/$BRANCH=$target"

if [ "$dirty" -gt 0 ]; then
  echo "  discarded $dirty local change(s) in the clone; canon is the only writer"
fi

# reset --hard writes files with the caller's umask, so a fresh file can land world-readable
# in a directory that was deliberately o-rwx. Restore the policy the clone was created with.
if [ "$(id -u)" -eq 0 ]; then
  chown -R "root:$RUNNER_USER" "$CLONE"
  # The venv is the runner's own writable area (pip installs into it), and the runner is
  # not root, so it keeps its ownership.
  if [ -d "$CLONE/venv" ]; then
    chown -R "$RUNNER_USER:$RUNNER_USER" "$CLONE/venv"
  fi
  chmod 0750 "$CLONE"
  chmod -R o-rwx "$CLONE"
else
  echo "  note: not root — ownership and permissions left as they are"
fi

# A changed requirements.txt means the venv no longer matches the code it runs, and the
# failure mode is an ImportError in a run that already looked healthy. Reinstall as the
# runner so the venv keeps the ownership pip expects.
reqs_after=$(sha256sum "$CLONE/ops/e2e/requirements.txt" 2>/dev/null | awk '{print $1}')
if [ -x "$CLONE/venv/bin/pip" ] && [ "$reqs_before" != "$reqs_after" ]; then
  echo "  requirements.txt changed — reinstalling into the venv"
  if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$RUNNER_USER" -- "$CLONE/venv/bin/pip" install -q -r "$CLONE/ops/e2e/requirements.txt" \
      || die "pip install failed; the venv does not match requirements.txt"
  else
    "$CLONE/venv/bin/pip" install -q -r "$CLONE/ops/e2e/requirements.txt" \
      || die "pip install failed; the venv does not match requirements.txt"
  fi
fi

if [ "$behind" -eq 0 ]; then
  echo "  clone already at origin/$BRANCH ($after)"
else
  echo "  clone $before -> $after ($behind commit(s) from origin/$BRANCH)"
fi
