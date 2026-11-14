#!/usr/bin/env bash
# reexec-if-changed.sh — run the copy of a script that was just synced, not the one bash
# started with.
#
# Why this exists: bash reads a script incrementally from its file. `sync-canon.sh` does
# `git reset --hard` and then keeps executing — and when the new commit changes
# sync-canon.sh itself, the running process continues with the old bytes while the file on
# disk is new. Every step added in that commit is silently skipped on the very sync that
# installs it. Observed 14.11: the Prometheus-reload step was merged, the sync printed
# "SYNC OK", and the step never ran; a second run executed it and found the rule missing.
# A gate that cannot fire on the sync that adds it is exactly the class of defect this
# repository keeps finding.
#
# Contract:
#   . ops/lib/reexec-if-changed.sh
#   reexec_if_changed <running-script> <synced-script> [args...]
#
# Returns 0 when the two files are identical (the caller continues). When they differ it
# execs the synced copy with the given arguments, so the rest of the run is the new code.
# SYNC_CANON_REEXEC=1 disables it: the child inherits the flag and would otherwise re-exec
# itself forever.
#
# Kept as a library rather than inline in sync-canon.sh so the mechanism can be tested
# without a real clone, a real docker daemon or root: the test feeds it two files.

reexec_if_changed() {
  local running="$1" target="$2"
  shift 2

  [ "${SYNC_CANON_REEXEC:-0}" = "1" ] && return 0
  [ -r "$running" ] && [ -r "$target" ] || return 0

  local running_hash target_hash
  running_hash=$(sha256sum "$running" | awk '{print $1}')
  target_hash=$(sha256sum "$target" | awk '{print $1}')
  [ "$running_hash" = "$target_hash" ] && return 0

  echo "  $(basename "$target") changed in this commit — re-executing the synced copy"
  SYNC_CANON_REEXEC=1 exec bash "$target" "$@"
}
