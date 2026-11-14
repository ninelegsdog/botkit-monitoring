#!/usr/bin/env bash
# CI gate: every TODO/FIXME comment must reference an issue number.
# Accepted: "TODO(#123)", "FIXME: #45", "TODO #7".
# Only comments count: a bare mention in a string or a doc sentence is ignored.
# TODO_SCAN_DIR exists so the gate itself can be tested on a fixture tree.
set -u

scan="${TODO_SCAN_DIR:-ops}"
self="$scan/lib/todo_check.sh"
bad=0

while IFS= read -r hit; do
  [ -z "$hit" ] && continue
  case "$hit" in
    "$self":*) continue ;;
  esac
  if printf '%s' "$hit" | grep -qE '(TODO|FIXME)[[:space:]]*:?[[:space:]]*\(?#[0-9]+'; then
    continue
  fi
  printf 'missing issue ref: %s\n' "$hit"
  bad=1
done < <(grep -rnE '(^|[[:space:]/])(#|//)[[:space:]]*(TODO|FIXME)(\(|:|[[:space:]]|$)' "$scan" \
           --include='*.sh' --include='*.py' --include='*.yml' 2>/dev/null || true)

if [ "$bad" -ne 0 ]; then
  echo "FAIL: TODO/FIXME without issue reference"
  exit 1
fi
echo "OK: every TODO/FIXME comment references an issue"
