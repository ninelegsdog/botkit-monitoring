#!/usr/bin/env bash
# External stop-cock: a plain GET to the self-hosted Uptime Kuma every two minutes.
#
# Every check on prod answers the same question - "can this host still run code
# and see the network" - which is exactly what stops being true when the host is
# gone. Prometheus died on 03.10 and stayed dead for two days precisely because
# nothing outside prod was watching; a heartbeat is the piece that keeps working
# when the machine cannot report on itself. Silence past the window is the
# signal, so the only job here is to keep proving liveness and never to stop.
#
# The watcher is Uptime Kuma running on the monitor (31.76.11.198) - a host
# outside prod that already holds the offsite backups. It was chosen over
# healthchecks.io because that service is unreachable from the owner's network
# in Russia (403 at the edge), and because nothing third-party has to see that
# our production is alive. The URL below points at prod's own loopback port and
# reaches Kuma only through botkit-kuma-tunnel.service, so the ping token never
# crosses the internet in the clear and no new port is open anywhere.
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
  echo "$(ts) FAIL нет ping-URL ($URL_FILE): push-монитор в Kuma не создан" >>"$LOG"
  exit 1
fi

url=$(head -n1 "$URL_FILE" | tr -d '[:space:]')
case "$url" in
  # Loopback only. The tunnel forwards this one port; anything else in the file
  # means the credential was replaced and must be treated as an incident.
  http://127.0.0.1:3001/api/push/*) ;;
  *)
    echo "$(ts) FAIL файл есть, но URL не похож на push-эндпоинт Kuma на loopback" >>"$LOG"
    exit 1
    ;;
esac

# A failed ping is itself the signal: without it Kuma counts the silence and
# announces the outage either way - including when the tunnel below is down.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$url" 2>>"$LOG")
if [ "$code" = "200" ]; then
  echo "$(ts) OK heartbeat отправлен" >>"$LOG"
  exit 0
fi

echo "$(ts) FAIL ping не прошёл http=$code (туннель botkit-kuma-tunnel или Kuma)" >>"$LOG"
exit 1
