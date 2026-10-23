#!/usr/bin/env bash
# asus-ssh-check.sh — мониторинг SSH на asus-1: sshd down / UFW LIMIT BLOCK.
# Запускается из git-клона botkit-monitoring (ops/ssh/), cron */5.
# Источник истины — git, не hand-copy (урок инцидента 03.10).

set -uo pipefail

SCRIPT_VERSION="1.0.0"
STATE_DIR="${HOME}/.local/state/asus-ssh-check"
ALERT_DIR="${STATE_DIR}/alerts"
LOG_FILE="${STATE_DIR}/check.log"
OFFSET_FILE="${STATE_DIR}/ufw.offset"
ENV_FILE="${HOME}/.config/asus-ssh-check/env"
UFW_LOG="/var/log/ufw.log"
THROTTLE_SEC=1800

mkdir -p "${STATE_DIR}" "${ALERT_DIR}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "${LOG_FILE}"; }

# Секреты — только из файла, не из argv/env
if [ -r "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
else
  log "ERROR env file missing: ${ENV_FILE}"
  exit 1
fi

if [ -z "${TG_BOT_TOKEN:-}" ] || [ -z "${TG_CHAT_ID:-}" ]; then
  log "ERROR TG_BOT_TOKEN or TG_CHAT_ID empty"
  exit 1
fi

now_s() { date +%s; }

send_telegram() { # $1=text
  local text="$1"
  local resp
  # Telegram заблокирован на asus-1 (DPI). Отправка через monitor (root),
  # который имеет доступ к Telegram. Токен читается из файла на monitor.
  resp=$(printf '%s' "${text}" | ssh -o ConnectTimeout=10 -o BatchMode=yes \
    -i "${HOME}/.ssh/id_ed25519.shared-bak" -o IdentitiesOnly=yes \
    root@31.76.11.198 \
    'source /root/.asus-alert/env && curl -sS --max-time 10 -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT_ID}" --data-urlencode "text@-" ' 2>>"${LOG_FILE}")
  if echo "${resp}" | grep -q '"ok":true'; then
    log "TG sent via monitor: ${text:0:80}"
    return 0
  fi
  log "TG send FAILED: ${resp:0:200}"
  return 1
}

throttled() { # $1=type -> 0 if OK to send
  local t="$1"
  local f="${ALERT_DIR}/${t}.ts"
  if [ -f "${f}" ]; then
    local age=$(( $(now_s) - $(stat -c %Y "${f}") ))
    if [ "${age}" -lt "${THROTTLE_SEC}" ]; then
      log "THROTTLED ${t} (age=${age}s)"
      return 0
    fi
  fi
  return 1
}

mark_alert() { # $1=type
  touch "${ALERT_DIR}/$1.ts"
}

alert() { # $1=type $2=text
  local t="$1" text="$2"
  if throttled "${t}"; then return 0; fi
  if send_telegram "${text}"; then
    mark_alert "${t}"
  fi
}

# --- Проверка 1: sshd active ---
sshd_ok=0
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
  sshd_ok=1
fi

# --- Проверка 2: TCP 22 ---
tcp_ok=0
if timeout 3 bash -c '</dev/tcp/127.0.0.1/22' 2>/dev/null; then
  tcp_ok=1
fi

if [ "${sshd_ok}" -ne 1 ] || [ "${tcp_ok}" -ne 1 ]; then
  alert "sshd_down" "[asus-1 SSH] sshd down
Хост: asus-1 (192.168.0.139)
Время: $(date -u '+%Y-%m-%d %H:%M:%S UTC')
sshd_active=${sshd_ok} tcp22=${tcp_ok}"
fi

# --- Проверка 3: UFW LIMIT BLOCK (новые) ---
cur_size=0
if [ -r "${UFW_LOG}" ]; then
  cur_size=$(stat -c %s "${UFW_LOG}" 2>/dev/null || echo 0)
fi

if [ ! -f "${OFFSET_FILE}" ]; then
  # Первый запуск: запомнить смещение, не алертить
  echo "${cur_size}" > "${OFFSET_FILE}"
  log "INIT offset=${cur_size}"
else
  old_offset=$(cat "${OFFSET_FILE}" 2>/dev/null || echo 0)
  # Ротация лога
  if [ "${cur_size}" -lt "${old_offset}" ]; then
    old_offset=0
    log "LOG ROTATED, reset offset"
  fi
  if [ "${cur_size}" -gt "${old_offset}" ]; then
    tmp=$(mktemp)
    tail -c +$((old_offset + 1)) "${UFW_LOG}" > "${tmp}" 2>/dev/null || true
    # фильтр по DPT=22
    limit_ssh=$(grep "UFW LIMIT BLOCK" "${tmp}" 2>/dev/null | grep -c "DPT=22" 2>/dev/null || true)
    limit_ssh=${limit_ssh:-0}
    if [ "${limit_ssh}" -gt 0 ]; then
      sample=$(grep "UFW LIMIT BLOCK" "${tmp}" | grep "DPT=22" | tail -3 | sed 's/.*SRC=/SRC=/;s/ .*//' | tr '\n' ' ')
      alert "ufw_limit_block" "[asus-1 SSH] UFW LIMIT BLOCK: ${limit_ssh} новых
Хост: asus-1 (192.168.0.139)
Время: $(date -u '+%Y-%m-%d %H:%M:%S UTC')
${sample}"
    fi
    rm -f "${tmp}"
  fi
  echo "${cur_size}" > "${OFFSET_FILE}"
fi

# --- Проверка 4 (опц.): fail2ban банит LAN ---
if command -v fail2ban-client >/dev/null 2>&1; then
  banned_lan=$(fail2ban-client status sshd 2>/dev/null | grep -oE '192\.168\.0\.[0-9]+' | head -1 || true)
  if [ -n "${banned_lan}" ]; then
    alert "fail2ban_lan_banned" "[asus-1 SSH] fail2ban забанил LAN IP: ${banned_lan}
Хост: asus-1 (192.168.0.139)
Время: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  fi
fi

log "OK sshd=${sshd_ok} tcp=${tcp_ok} offset=${cur_size} v=${SCRIPT_VERSION}"
exit 0
