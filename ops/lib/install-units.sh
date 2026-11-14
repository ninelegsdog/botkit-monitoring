#!/usr/bin/env bash
# install-units.sh — put the canon systemd units on the host, then prove they match.
#
# The sync updated every script and no unit file at all. On 14.11 the installed
# botkit-rollout-check.service still carried the ReadWritePaths list from before the
# textfile metric existed: the poller ran, checked nine bots, and then died on
# `mktemp: Read-only file system` while publishing its verdict. BotkitRolloutCheckStale
# fired, and that rule is `absent(...) or (time() - metric > 900)` — it cannot tell a dead
# poller from a unit that cannot write its own metric, so the alert was indistinguishable
# from the outage it was supposed to catch. Canon had the correct path; the host had the old
# file; nothing compared them.
#
# Scope comes from ops/lib/managed-units.txt, not from a glob: installing everything under
# ops/*/systemd would deploy units that describe hosts or runners that do not exist here
# (botkit-e2e needs an unprivileged user and /opt/botkit-e2e) and would replace a direct
# ExecStart with a symlink for check-backs. The manifest makes each host change reviewable.
#
# Behaviour per managed unit:
#   identical          → nothing
#   differs            → backup, verify, install
#   absent on the host → report only; provisioning is host-setup's job, and installing a
#                        unit without enabling it changes nothing
#
# --check reports the same drift, changes nothing and exits 1. The sync uses the install
# path and then re-compares, so "SYNC OK" means the running definitions are the canon ones.
#
# Usage: install-units.sh [--check]
set -uo pipefail

REPO=${REPO:-/home/deploy/botkit-monitoring}
DEST=${UNIT_DEST:-/etc/systemd/system}
MANIFEST=${UNIT_MANIFEST:-$REPO/ops/lib/managed-units.txt}
STAMP=${UNIT_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}

CHECK=0
case "${1:-}" in
  "") ;;
  --check) CHECK=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

[ -r "$MANIFEST" ] || { echo "FATAL: manifest $MANIFEST is missing" >&2; exit 1; }
[ -d "$DEST" ] || { echo "FATAL: unit directory $DEST is missing" >&2; exit 1; }

# Resolve a unit basename to the single file canon ships for it. Two copies of one basename
# in different ops/*/systemd directories would make "the canon file" ambiguous, and a guess
# here is how the wrong unit gets installed.
canon_path() {
  local name="$1" found="" hit
  for hit in "$REPO"/ops/*/systemd/"$name"; do
    [ -f "$hit" ] || continue
    if [ -n "$found" ]; then
      echo "FATAL: canon ships $name twice: $found and $hit" >&2
      return 1
    fi
    found="$hit"
  done
  [ -n "$found" ] || { echo "FATAL: manifest names $name but canon ships no such unit" >&2; return 1; }
  printf '%s\n' "$found"
}

drift=0
changed=()
failed=0

while IFS= read -r name || [ -n "$name" ]; do
  case "$name" in ''|'#'*) continue ;; esac
  # Strip trailing whitespace and CR so a CRLF edit does not turn into a phantom unit name.
  name="${name%%[[:space:]]*}"
  [ -n "$name" ] || continue

  src=$(canon_path "$name") || exit 1
  target="$DEST/$name"

  if [ ! -f "$target" ]; then
    echo "  absent  $name — not installed on this host (provisioning is host-setup's job)"
    continue
  fi

  if cmp -s "$src" "$target"; then
    echo "  ok      $name"
    continue
  fi

  if [ "$CHECK" = 1 ]; then
    echo "  DRIFT   $name — $target differs from $src"
    drift=$((drift + 1))
    continue
  fi

  # Verify before the host sees it: a unit that fails to parse takes down daemon-reload, and
  # a broken ExecStart is cheaper to find here than in `systemctl --failed` at 06:15.
  if command -v systemd-analyze >/dev/null 2>&1; then
    if ! systemd-analyze verify "$src" >/dev/null 2>&1; then
      echo "FATAL: systemd-analyze rejects $src; not installing it" >&2
      systemd-analyze verify "$src" >&2 2>&1 || true
      failed=$((failed + 1))
      continue
    fi
  fi

  cp -p "$target" "$target.bak-$STAMP" || { echo "FATAL: cannot back up $target" >&2; failed=$((failed + 1)); continue; }
  install -m 0644 "$src" "$target" || { echo "FATAL: cannot install $target" >&2; failed=$((failed + 1)); continue; }
  echo "  install $name (backup $name.bak-$STAMP)"
  changed+=("$name")
done < "$MANIFEST"

if [ "$CHECK" = 1 ]; then
  if [ "$drift" -gt 0 ]; then
    echo "FATAL: $drift managed unit(s) differ from canon; run $0 to install them" >&2
    exit 1
  fi
  echo "  ok: every installed managed unit matches canon"
  exit 0
fi

if [ "${#changed[@]}" -gt 0 ]; then
  # One reload for the whole batch. Without it systemd keeps the old definitions in memory
  # and the next timer tick runs the unit that was just replaced on disk.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || { echo "FATAL: systemctl daemon-reload failed" >&2; exit 1; }
  fi
  # A changed timer keeps its old schedule until it is restarted: systemd caches the calendar
  # spec at activation, so daemon-reload alone would leave the previous OnCalendar running
  # until the next boot. Only timers that are active are restarted — enabling stays host work.
  for name in "${changed[@]}"; do
    case "$name" in
      *.timer)
        if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$name"; then
          systemctl restart "$name" || { echo "FATAL: cannot restart $name" >&2; failed=$((failed + 1)); }
        fi
        ;;
    esac
  done
fi

# Post-conditions: the files on disk are the canon ones. A copy that silently did not land
# (read-only /etc, wrong DEST) is exactly the failure this script exists to prevent, so it
# must not be reported as a successful sync.
for name in "${changed[@]}"; do
  src=$(canon_path "$name") || exit 1
  cmp -s "$src" "$DEST/$name" || { echo "FATAL: $DEST/$name still differs after install" >&2; failed=$((failed + 1)); }
done

if [ "$failed" -gt 0 ]; then
  echo "FATAL: $failed unit operation(s) failed" >&2
  exit 1
fi

if [ "${#changed[@]}" -gt 0 ]; then
  echo "  installed ${#changed[@]} unit(s): ${changed[*]}"
else
  echo "  ok: every installed managed unit matches canon"
fi
exit 0
