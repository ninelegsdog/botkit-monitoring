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

echo "SYNC OK $head_sha"
