#!/usr/bin/env bash
# reexec-if-changed.sh — run the copy of a script that was just synced, not the one bash
# started with.
#
# Why this exists: bash reads a script from its file, and `sync-canon.sh` does
# `git reset --hard` and then keeps executing. When the new commit changes sync-canon.sh
# itself, the rest of the run is the old code while the clone holds the new one, so every
# step added in that commit is skipped on the very sync that installs it. A gate that cannot
# fire on the sync that adds it is exactly the class of defect this repository keeps finding.
#
# The hash has to be taken BEFORE the reset, by the caller:
#
#   SELF_HASH=$(sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}')
#   ... reset the clone ...
#   reexec_if_changed "$SELF_HASH" "$REPO/ops/lib/sync-canon.sh" [args...]
#
# The first version of this helper took the running script's *path* instead and hashed it
# after the reset — which is the same file as the target, so the two hashes always matched
# and the re-exec never happened. Observed 14.11: the Prometheus-reload step was merged,
# the sync printed "SYNC OK", the step did not run, and a second run executed it. A helper
# that compares a file with itself cannot detect that the file changed.
#
# Contract:
#   reexec_if_changed <hash-of-the-running-script> <synced-script> [args...]
#
# Returns 0 when the synced copy is byte-identical to what is executing. Otherwise it execs
# the synced copy with the given arguments, so the rest of the run is the new code.
# SYNC_CANON_REEXEC=1 disables it: the child inherits the flag and would otherwise re-exec
# itself forever.
#
# Kept as a library rather than inline in sync-canon.sh so the mechanism can be tested
# without a real clone, a real docker daemon or root: the test feeds it two files.

reexec_if_changed() {
  local running_hash="$1" target="$2"
  shift 2

  [ "${SYNC_CANON_REEXEC:-0}" = "1" ] && return 0
  [ -n "$running_hash" ] && [ -r "$target" ] || return 0

  local target_hash
  target_hash=$(sha256sum "$target" | awk '{print $1}')
  [ "$running_hash" = "$target_hash" ] && return 0

  echo "  $(basename "$target") changed in this commit — re-executing the synced copy"
  SYNC_CANON_REEXEC=1 exec bash "$target" "$@"
}
