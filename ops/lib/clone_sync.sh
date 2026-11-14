#!/usr/bin/env bash
# clone_sync.sh — синхронизация git-клона с origin/main с проверкой.
#
# Паттерн: fetch → проверка HEAD != origin/main → reset --hard → проверка.
# Если clone уже актуален — ничего не делает (idempotent).
# При ошибке fetch — возвращает 1 (не молчит).
#
# Использование:
#   source ops/lib/clone_sync.sh
#   sync_clone "/path/to/clone" "main" || { echo "sync failed"; exit 1; }
set -u

sync_clone() {
  local clone_dir="$1"
  local branch="${2:-main}"
  local before after target

  [ -d "$clone_dir/.git" ] || { echo "clone_sync: $clone_dir is not a git clone" >&2; return 1; }

  before=$(git -C "$clone_dir" rev-parse --short HEAD 2>/dev/null) || return 1

  git -C "$clone_dir" fetch --prune origin "$branch" >/dev/null 2>&1 || {
    echo "clone_sync: fetch failed for $clone_dir" >&2
    return 1
  }

  target=$(git -C "$clone_dir" rev-parse --short "origin/$branch" 2>/dev/null) || return 1

  if [ "$before" = "$target" ]; then
    return 0  # already current
  fi

  git -C "$clone_dir" reset --hard "origin/$branch" >/dev/null 2>&1 || return 1

  after=$(git -C "$clone_dir" rev-parse --short HEAD 2>/dev/null) || return 1
  [ "$after" = "$target" ] || { echo "clone_sync: HEAD=$after != origin/$branch=$target" >&2; return 1; }

  echo "clone_sync: $clone_dir $before -> $after"
  return 0
}
