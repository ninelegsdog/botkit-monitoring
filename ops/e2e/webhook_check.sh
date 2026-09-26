#!/usr/bin/env bash
#
# webhook_check.sh — тонкая обёртка над webhook_check.py для systemd.
# Смысл в отдельном файле: путь ExecStart не меняется при правках python-части,
# а каталог установки (/root/botkit-webhook-check) читается относительно себя.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WEBHOOK_CHECK_PYTHON:-/usr/bin/python3}"

if [ ! -f "$HERE/webhook_check.py" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FATAL: $HERE/webhook_check.py missing" >&2
  exit 1
fi

exec "$PY" "$HERE/webhook_check.py" "$@"
