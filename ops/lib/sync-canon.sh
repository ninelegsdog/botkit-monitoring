#!/usr/bin/env bash
#
# sync-canon.sh — синхронизировать канон botkit-monitoring на прод и ДОКАЗАТЬ, что
# синк состоялся. Запускать от root.
#
#   bash ops/lib/sync-canon.sh [expected-commit|HEAD]
#
# Почему не `git fetch --quiet && git reset --hard` одной строкой:
#   1) fetch от пользователя deploy падает с "insufficient permission for adding an
#      object to repository database", если раньше git запускался от root и оставил
#      root:root объекты. С --quiet ошибка не видна, reset --hard молча остаётся на
#      старом коммите — и раскатка «проходит», ничего не меняя. Именно так 26.09
#      потерялся job webhook-trusted: promtool показал 18 правил вместо 19.
#   2) Воркtree /home/deploy/botkit-monitoring принадлежит deploy:deploy, как и все
#      остальные воркtreeы бота. Git здесь всегда от deploy, иначе ownership снова
#      разъедется.
#   3) После reset проверяем маркеры содержимого, а не только HEAD: reset успешен и
#      при старом содержимом, если origin/main не обновился.
#
set -euo pipefail

REPO=${REPO:-/home/deploy/botkit-monitoring}
GIT_USER=${GIT_USER:-deploy}
# --check-only runs the content assertions (steps 4 and 4b) against REPO and touches no
# git state. It exists so the assertions can be exercised on a scratch copy - the only
# way to show a gate fails is to feed it the defect it is supposed to catch.
CHECK_ONLY=0
if [ "${1:-}" = "--check-only" ]; then
  CHECK_ONLY=1
  shift
fi
EXPECT_REF="${1:-HEAD}"
MARKERS=(
  "prometheus/blackbox.yml:tls_trusted"
  "prometheus/prometheus.yml:job_name: webhook-trusted"
  "prometheus/alerts.yml:BotWebhookTLSUntrusted"
  # S1: fleet.env is now the single source for every monitoring address, and the
  # whole point of a marker is that HEAD alone does not prove the sync took. A
  # production copy of fleet.env without the new maps would make six scripts fall
  # back to nothing, so its contents are asserted here too.
  "ops/lib/fleet.env:ALERTMANAGER_ALERTS_URL"
  "ops/lib/fleet.env:WG_OBSERVE_SUBNET"
  "ops/e2e/smoke_all.sh:ALERTMANAGER_ALERTS_URL"
  "ops/e2e/webhook_check.py:ALERTMANAGER_ALERTS_URL"
)

[ -d "$REPO" ] || { echo "FATAL: $REPO missing" >&2; exit 1; }

if [ "$CHECK_ONLY" = 1 ]; then
  echo "CHECK-ONLY: content assertions only, git untouched"
  head_sha=$(git -c safe.directory="$REPO" -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)
else
echo "=== 1. normalise ownership ($GIT_USER:$GIT_USER) ==="
chown -R "$GIT_USER:$GIT_USER" "$REPO"

echo "=== 2. fetch as $GIT_USER (no --quiet, failures must be visible) ==="
su -s /bin/bash "$GIT_USER" -c "git -c safe.directory='$REPO' -C '$REPO' fetch origin main"

echo "=== 3. reset --hard as $GIT_USER ==="
su -s /bin/bash "$GIT_USER" -c "git -c safe.directory='$REPO' -C '$REPO' reset --hard origin/main"
head_sha=$(git -c safe.directory="$REPO" -C "$REPO" rev-parse --short HEAD)
echo "  HEAD = $head_sha"
fi

if [ "$EXPECT_REF" != "HEAD" ] && [ "$head_sha" != "${EXPECT_REF:0:7}" ]; then
  echo "FATAL: HEAD=$head_sha, expected ${EXPECT_REF:0:7}" >&2
  exit 1
fi

echo "=== 4. content markers (HEAD alone does not prove the sync) ==="
for marker in "${MARKERS[@]}"; do
  file="${marker%%:*}"
  needle="${marker#*:}"
  if ! grep -q "$needle" "$REPO/$file"; then
    echo "FATAL: $file does not contain '$needle' — sync did not take" >&2
    exit 1
  fi
  echo "  ok: $file has '$needle'"
done

# S2: node-exporter used to mount a tmpfs on /tmp. With --path.rootfs=/host the
# collector prefixes every mountpoint with the rootfs, so the container's own /tmp and
# the host's /tmp collapse into one identical label set and node_exporter rejects the
# second - 7 duplicate-metric errors on every scrape. Markers above assert that content
# arrived; this one asserts that something was *removed*, which no marker can do.
echo "=== 4b. node-exporter must not mount a tmpfs on /tmp ==="
ne_block=$(awk '/^  node-exporter:/{f=1;next} /^  [a-zA-Z]/{f=0} f' "$REPO/docker-compose.yml")
if printf "%s\n" "$ne_block" | grep -qE '^[[:space:]]*- /tmp[[:space:]]*$'; then
  echo "FATAL: node-exporter mounts tmpfs /tmp again - duplicate node_filesystem_* series." >&2
  echo "       Remove it from docker-compose.yml; see the comment in the node-exporter block." >&2
  exit 1
fi
echo "  ok: no /tmp tmpfs on node-exporter"

echo "=== 4c. every ops script must parse ==="
# The timers run these scripts straight out of this repository, so a syntax error in a canon
# commit is not caught by anything else here: the unit would fail on every run, or - worse -
# exit early and look like a clean check. Assert that each one parses before declaring the
# sync good, the same way step 4 asserts that content arrived.
while IFS= read -r -d '' script; do
  if ! bash -n "$script" 2>/dev/null; then
    echo "FATAL: $script does not parse - refusing to report SYNC OK" >&2
    bash -n "$script" || true
    exit 1
  fi
  echo "  ok: ${script#"$REPO"/} parses"
done < <(find "$REPO/ops" -name '*.sh' -print0 | sort -z)

echo "=== 4d. bot data mounts must stay owned by the container user ==="
# The bulk `chown -R deploy:deploy` that normalises this repository has, three times now,
# recursed into the *bot* clones and taken data/ and backups/ from 1001:1001 (the compose
# `user: "1001:1001"`) to 1000:1000 (deploy). The bots then could not open their own
# database while /health still answered 200, so the outage was invisible from outside -
# it only surfaced when a container was recreated.
#
# compose pins the runtime user but not the directory owner, so this is the check that
# notices. It is cheap, runs on every canon sync, and fails the sync instead of leaving a
# half-broken fleet behind. deploy_rollout.sh's preflight still guards a single rollout;
# this guards the fleet even when nothing was being rolled out.
_fleet_bot_roots() {
  local d
  for d in /home/deploy/botkit-*; do
    [ -d "$d/data" ] && printf '%s\n' "$d"
  done
}

_data_fail=0
_roots=$(_fleet_bot_roots)
if [ -z "$_roots" ]; then
  echo "  ok: no bot data mounts found (nothing to assert)"
else
  while read -r root; do
    [ -n "$root" ] || continue
    for mount in data backups; do
      dir="$root/$mount"
      [ -d "$dir" ] || continue
      duid=$(stat -c %u "$dir" 2>/dev/null || echo "?")
      if [ "$duid" != "1001" ]; then
        echo "FATAL: $dir is owned by uid=$duid, expected 1001 (compose user 1001:1001)." >&2
        echo "       Bots cannot open their database while /health answers 200." >&2
        echo "       Fix: chown -R 1001:1001 $dir" >&2
        _data_fail=1
      fi
    done
  done <<<"$_roots"
  [ "$_data_fail" -eq 0 ] && echo "  ok: every bot data/ and backups/ mount is uid 1001"
fi
[ "$_data_fail" -eq 0 ] || exit 1

echo "SYNC OK $head_sha"
