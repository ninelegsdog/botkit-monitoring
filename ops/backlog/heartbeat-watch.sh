#!/usr/bin/env bash
# Сторож реплики BOTKIT_BACKLOG на мониторе: heartbeat predator → push в Uptime Kuma.
#
# Почему на мониторе, а не на predator: молчание как сигнал работает только тогда,
# когда наблюдает кто-то, кто не падает вместе с наблюдаемым. predator — хост с
# репликой; монитор — внешний по отношению к нему и уже держит offsite-бэкапы, а
# Kuma на нём — тот же внешний стоп-кран, которым прод сообщает о своей живости.
#
# predator кладёт сюда файл состояния через sftp (ops/backlog/replicate.sh), этот
# скрипт переводит его в up/down. Push-URL Kuma — носитель прав: кто им владеет,
# тот молчание подделает, поэтому файл root-only, и в лог URL не попадает.
set -uo pipefail

HB=${HB:-/srv/botkit-backup/heartbeat/botkit-backlog.json}
URL_FILE=${URL_FILE:-/root/.botkit-backlog/kuma-push.url}
LOG=${LOG:-/var/log/botkit-backlog-heartbeat.log}
# Порог молчания. Репликация идёт каждый час; три часа — это два пропущенных
# прогона, то есть сбой, а не случайный пропуск.
MAXAGE=${MAXAGE:-10800}
TIMEOUT=10

ts() { date -u +%FT%TZ; }
mkdir -p "$(dirname "$LOG")"

# Вердикт целиком в одном месте: bash только отправляет. Разбор JSON на bash —
# это молчаливые ошибки на многострочном файле, а heartbeat пишет python.
verdict=$(python3 - "$HB" "$MAXAGE" <<'PY' 2>>"$LOG"
import json, sys, datetime

path, maxage = sys.argv[1], int(sys.argv[2])

def out(status, msg):
    print(f"{status}\t{msg}")
    sys.exit(0)

try:
    with open(path, encoding="utf-8") as f:
        hb = json.load(f)
except FileNotFoundError:
    out("down", "heartbeat отсутствует: реплика не отчиталась ни разу")
except Exception as exc:  # noqa: BLE001 - любая нечитаемость = сбой реплики
    out("down", f"heartbeat нечитаем ({path}): {exc}")

try:
    t = datetime.datetime.fromisoformat(str(hb["ts"]).replace("Z", "+00:00"))
except Exception:  # noqa: BLE001
    out("down", "heartbeat без корректного ts")
    sys.exit(0)

age = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
snaps = ",".join(f"{k}={v}" for k, v in sorted((hb.get("snapshots") or {}).items())) or "нет"
fact = (f"age={age}s snapshots={snaps} "
        f"bucket={hb.get('bucket_bytes', 0)}B free={hb.get('disk_free_bytes', 0)}B")

if age > maxage:
    out("down", f"heartbeat устарел: {fact}")
if int(hb.get("mount_ok", 0)) != 1 or int(hb.get("device_ok", 0)) != 1:
    out("down", f"хранилище не то (mount_ok={hb.get('mount_ok')} "
                f"device_ok={hb.get('device_ok')}, reason={hb.get('reason', '?')})")
if int(hb.get("ok", 0)) != 1:
    out("down", f"прогон с ошибкой ({hb.get('reason', '?')}): {fact}")
out("up", fact)
PY
)
status=down
msg="сторож не смог разобрать heartbeat"
if [ -n "$verdict" ]; then
  IFS=$'\t' read -r status msg <<<"$verdict"
fi

if [ ! -r "$URL_FILE" ]; then
  echo "$(ts) FAIL статус реплики «$status», но push-URL Kuma не заведён ($URL_FILE): тревога не поднята" >>"$LOG"
  exit 1
fi
url=$(head -n1 "$URL_FILE" | tr -d '[:space:]')
case "$url" in
  # Только loopback: Kuma слушает на 127.0.0.1:3001, наружу порт не выставлен.
  # Что-то другое в файле — подменённый носитель прав, и это инцидент, а не настройка.
  http://127.0.0.1:3001/api/push/*) ;;
  *)
    echo "$(ts) FAIL push-URL не похож на эндпоинт Kuma на loopback" >>"$LOG"
    exit 1
    ;;
esac

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" \
  --get "$url" --data-urlencode "status=$status" --data-urlencode "msg=$msg" 2>>"$LOG")
if [ "$code" = "200" ]; then
  echo "$(ts) OK push=$status ($msg)" >>"$LOG"
  exit 0
fi
echo "$(ts) FAIL push не прошёл http=$code (Kuma или push-URL)" >>"$LOG"
exit 1
