#!/usr/bin/env bash
# External stop-cock: a plain GET to healthchecks.io every five minutes.
#
# Every check on prod answers the same question - "can this host still run code
# and see the network" - which is exactly what stops being true when the host is
# gone. Prometheus died on 03.10 and stayed dead for two days precisely because
# nothing outside prod was watching; a heartbeat is the piece that keeps working
# when the machine cannot report on itself. Silence past the grace period is the
# signal, so the only job here is to keep proving liveness and never to stop.
#
# The ping URL is a bearer credential: whoever holds it can keep the monitor
# quiet. It lives in a root-only file, and the copy belongs in the password
# manager next to the backup keys - losing it only costs a new check, hiding it
# costs the alert.
set -u

URL_FILE="${BOTKIT_HEARTBEAT_URL_FILE:-/root/.botkit-heartbeat/ping.url}"
LOG="${BOTKIT_HEARTBEAT_LOG:-/var/log/botkit-heartbeat.log}"
TIMEOUT=10

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
mkdir -p "$(dirname "$LOG")"

# Never log the URL: the log is read by more eyes than the URL file is.
if [ ! -r "$URL_FILE" ]; then
  echo "$(ts) FAIL нет ping-URL ($URL_FILE): чек на healthchecks.io не создан" >>"$LOG"
  exit 1
fi

url=$(head -n1 "$URL_FILE" | tr -d '[:space:]')
case "$url" in
  https://hc-ping.com/* | https://healthchecks.io/check/*) ;;
  *)
    echo "$(ts) FAIL файл есть, но URL не похож на ping healthchecks.io" >>"$LOG"
    exit 1
    ;;
esac

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$url" 2>>"$LOG")
if [ "$code" = "200" ]; then
  echo "$(ts) OK heartbeat отправлен" >>"$LOG"
  exit 0
fi

# A failed ping is itself the signal: healthchecks.io starts counting the
# silence from the last successful one, so the outage is announced either way.
echo "$(ts) FAIL ping не прошёл http=$code" >>"$LOG"
exit 1
