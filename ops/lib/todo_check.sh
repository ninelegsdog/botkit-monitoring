#!/usr/bin/env bash
# CI check: fail if TODO/FIXME comments exist without a corresponding issue.
# Usage: bash ops/lib/todo_check.sh
set -u
bad=0
while IFS= read -r line; do
  file=$(echo "$line" | cut -d: -f1)
  match=$(echo "$line" | cut -d: -f2-)
  # Extract issue number if present (e.g., TODO(#123))
  if echo "$match" | grep -qE 'TODO\(?#?[0-9]+\)?'; then
    continue  # has issue reference
  fi
  echo "TODO/FIXME without issue: $line"
  bad=1
done < <(grep -rn "TODO\|FIXME" ops/ --include="*.sh" --include="*.py" --include="*.yml" 2>/dev/null || true)

if [ "$bad" -ne 0 ]; then
  echo "FAIL: TODO/FIXME without issue reference found"
  exit 1
fi
echo "OK: no untracked TODO/FIXME"
