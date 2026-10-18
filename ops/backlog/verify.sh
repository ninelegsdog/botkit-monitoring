#!/usr/bin/env bash
# Приёмка WORM-реплики BOTKIT_BACKLOG на predator. Запускается там же, где
# репликация, и не требует root.
#
# Зачем отдельный скрипт: «репликация идёт» и «реплика защищена» — разные факты.
# Первый виден в логе прогона, второй складывается из режима Object Lock, границ
# доступа, монтирования и совпадения счётчиков со источником. Раз в квартал это
# надо проверять целиком, и проверка должна уметь падать: код возврата
#   0 — всё проверено, дефектов нет
#   1 — есть дефект (см. FAIL)
#   2 — проверить нечем (нет инструмента/доступа), это НЕ «всё хорошо»
set -uo pipefail

SECRETS=${SECRETS:-$HOME/.config/botkit-backlog}
R=${RESTIC:-$HOME/.local/bin/restic}
PY=${PY:-$HOME/.local/venvs/s3admin/bin/python}
S3=${S3:-s3:http://127.0.0.1:9000/botkit-backlog}
ENDPOINT=${ENDPOINT:-http://127.0.0.1:9000}
BUCKET=${BUCKET:-botkit-backlog}
STATEDIR=${STATEDIR:-$HOME/.local/state/botkit-backlog}
HB=$STATEDIR/heartbeat.json
MOUNT=${MOUNT:-/mnt/storage}
DATA=${DATA:-/mnt/storage/BOTKIT_BACKLOG/minio-data}
CONTAINER=${CONTAINER:-botkit-backlog-minio}
IMAGE_PIN=${IMAGE_PIN:-bitnamilegacy/minio@sha256:451fe6858cb770cc9d0e77ba811ce287420f781c7c1b806a386f6896471a349c}
SRC_ALIAS=${SRC_ALIAS:-botkit-backlog-src}
HB_REMOTE=${HB_REMOTE:-/heartbeat/botkit-backlog.json}
REPOS=${REPOS:-"data cp monitor"}
HB_MAXAGE=${HB_MAXAGE:-10800}
CHECK_READ_DATA=0
[ "${1:-}" = "--full" ] && CHECK_READ_DATA=1

fails=0
unverified=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; unverified=$((unverified + 1)); }
note() { printf '      %s\n' "$*"; }

# source-пароли берём здесь же: они лежат рядом с target.pw и нужны для сверки
# счётчиков с источником и для проверки, что реплику они НЕ открывают.
# shellcheck disable=SC1090
set -a; . "$SECRETS/minio.env"; set +a
export AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
export AWS_DEFAULT_REGION=us-east-1
export RESTIC_PASSWORD_FILE="$SECRETS/target.pw"

echo "== приёмка реплики BOTKIT_BACKLOG ($(date -u +%FT%TZ)) =="

# --- 1. Хранилище: примонтировано, и контейнер видит ИМЕННО его ------------------
if mountpoint -q "$MOUNT"; then
  pass "$MOUNT примонтирован"
  host_dev=$(stat -c %d "$DATA" 2>/dev/null || echo "")
  ctr_dev=$(docker exec "$CONTAINER" bash -c 'stat -c %d /data' 2>/dev/null | tr -d '\r' || echo "")
  if [ -n "$host_dev" ] && [ "$host_dev" = "$ctr_dev" ]; then
    pass "контейнер видит то же устройство, что $DATA (device=$host_dev)"
  else
    fail "устройства разошлись: host=${host_dev:-нет} контейнер=${ctr_dev:-нет} — MinIO пишет не в NTFS"
  fi
else
  fail "$MOUNT не примонтирован — проверять реплику негде"
fi

# --- 2. Контейнер: жив, закалён, образ по пину ----------------------------------
if [ "$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)" = "running" ]; then
  pass "контейнер $CONTAINER running (up $(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER"))"
else
  fail "контейнер $CONTAINER не running"
fi
cfg=$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}} {{index .HostConfig.CapDrop 0}} {{.HostConfig.SecurityOpt}}' "$CONTAINER" 2>/dev/null)
case "$cfg" in
  "true 1000:1000 ALL [no-new-privileges:true]") pass "закалка: read_only, user 1000:1000, cap_drop ALL, no-new-privileges" ;;
  *) fail "закалка контейнера не та: $cfg" ;;
esac
img=$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || echo "")
if [ -n "$img" ] && docker image inspect "$img" --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null | grep -qx "$IMAGE_PIN"; then
  pass "образ совпадает с пином по digest"
else
  fail "образ не совпадает с пином ($IMAGE_PIN)"
fi

# --- 3. MinIO не выставлен наружу ------------------------------------------------
exposed=$(ss -ltnH 2>/dev/null | awk '$4 ~ /:(9000|9001)$/ {print $4}' | grep -v '^127\.0\.0\.1:' || true)
if [ -z "$exposed" ]; then
  pass "9000/9001 слушают только на 127.0.0.1"
else
  fail "порты MinIO видны наружу: $(echo "$exposed" | tr '\n' ' ')"
fi

# --- 4. Object Lock: режим, срок и версии под замком ----------------------------
if [ -x "$PY" ]; then
  out=$("$PY" - "$ENDPOINT" "$BUCKET" <<'PY' 2>&1
import datetime, sys
import boto3, botocore
ep, bucket = sys.argv[1], sys.argv[2]
s3 = boto3.client("s3", endpoint_url=ep)
now = datetime.datetime.now(datetime.timezone.utc)

v = s3.get_bucket_versioning(Bucket=bucket).get("Status")
print(f"versioning={v}")
lock = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
rule = lock["Rule"]["DefaultRetention"]
print(f"default_mode={rule.get('Mode')} days={rule.get('Days')}")

for prefix in ("data/", "cp/", "monitor/"):
    objs = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents") or []
    if not objs:
        print(f"prefix={prefix} объектов=0")
        continue
    key = objs[0]["Key"]
    versions = s3.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=1).get("Versions") or []
    if not versions:
        print(f"prefix={prefix} key={key} версий=0")
        continue
    head = s3.head_object(Bucket=bucket, Key=key, VersionId=versions[0]["VersionId"])
    mode = head.get("ObjectLockMode")
    until = head.get("ObjectLockRetainUntilDate")
    future = bool(until and until > now)
    print(f"prefix={prefix} mode={mode} retain_until={until and until.date()} в_будущем={future}")
PY
)
  if printf '%s' "$out" | grep -q 'versioning=Enabled'; then
    pass "versioning Enabled (версии не теряются при delete-marker)"
  else
    fail "versioning не Enabled: $(printf '%s' "$out" | head -1)"
  fi
  if printf '%s' "$out" | grep -q 'default_mode=COMPLIANCE days=90'; then
    pass "Object Lock по умолчанию: COMPLIANCE 90 дней"
  else
    fail "Object Lock не COMPLIANCE/90: $(printf '%s' "$out" | sed -n 2p)"
  fi
  bad=$(printf '%s' "$out" | grep -E 'mode=(None|GOVERNANCE)|в_будущем=False|объектов=0|версий=0' || true)
  if [ -z "$bad" ]; then
    pass "образцы в data/, cp/, monitor/ под COMPLIANCE с удержанием в будущем"
  else
    fail "объекты без замка: $(echo "$bad" | tr '\n' ';')"
  fi
  note "$(printf '%s' "$out" | grep '^prefix=' | tr '\n' ' ')"
else
  skip "нет $PY — режим Object Lock не проверен (объекты и сроки)"
fi

# --- 5. Счётчики совпадают с источником, а локи не залежались -------------------
snaps_target() {
  "$R" -r "$S3/$1" snapshots --json 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
}
snaps_source() {
  "$R" -r "sftp:$SRC_ALIAS:/repo-$1" --password-file "$SECRETS/$1.pw" snapshots --json 2>/dev/null \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
}
for repo in $REPOS; do
  t=$(snaps_target "$repo"); s=$(snaps_source "$repo")
  if [ -n "$t" ] && [ "$t" = "$s" ]; then
    pass "$repo: снапшотов в WORM $t = в источнике"
  elif [ -z "$t" ] || [ "$t" = "0" ]; then
    fail "$repo: в WORM ${t:-нет} снапшотов (источник $s)"
  else
    fail "$repo: расхождение — в WORM $t, в источнике $s (реплика отстала)"
  fi
  # Пароли источника реплику открывать не должны: иначе защита от стирания
  # держится на честном слове, а не на режиме хранилища.
  if "$R" -r "$S3/$repo" --password-file "$SECRETS/$repo.pw" snapshots >/dev/null 2>&1; then
    fail "$repo: пароль источника ОТКРЫВАЕТ WORM-реплику"
  else
    pass "$repo: пароль источника реплику не открывает"
  fi
done
# Зависший эксклюзивный лок блокирует `check` и restore, а в журнале не виден:
# прогон, снятый по SIGPIPE, оставляет лок на час. Ноль локов — норма.
locks=0
lockcheck_ok=1
for repo in $REPOS; do
  if out=$("$R" -r "$S3/$repo" list locks 2>/dev/null); then
    n=$(printf '%s\n' "$out" | grep -c . || true)
    locks=$((locks + n))
  else
    lockcheck_ok=0
  fi
done
if [ "$lockcheck_ok" = 0 ]; then
  fail "не удалось прочитать список локов (репозиторий недоступен)"
elif [ "$locks" -eq 0 ]; then
  pass "залежавшихся локов нет ни в одном репозитории"
else
  fail "локов в реплике: $locks (проверьте, не завис ли прогон)"
fi

# --- 6. Объём и место -----------------------------------------------------------
bytes=$(du -sb "$DATA/$BUCKET" 2>/dev/null | cut -f1 || echo 0)
free=$(df --output=avail -B1 "$MOUNT" 2>/dev/null | tail -n1 || echo 0)
pct=$(( free > 0 ? 100 * bytes / (bytes + free) : 100 ))
note "бакет $((bytes / 1048576)) МиБ, свободно $((free / 1073741824)) ГиБ (занято ${pct}%)"
if [ "$free" -gt 10737418240 ]; then
  pass "свободного места больше 10 ГиБ"
else
  fail "свободного места меньше 10 ГиБ: $((free / 1048576)) МиБ"
fi

# --- 7. Heartbeat: свежий, успешный и доехал до сторожа -------------------------
if [ -r "$HB" ]; then
  line=$(python3 - "$HB" "$HB_MAXAGE" <<'PY'
import json, sys, datetime
hb = json.load(open(sys.argv[1], encoding="utf-8"))
age = int((datetime.datetime.now(datetime.timezone.utc)
           - datetime.datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))).total_seconds())
print(f"{age} {hb.get('ok')} {hb.get('mount_ok')} {hb.get('device_ok')} {hb.get('bucket_bytes')}")
PY
)
  read -r age ok m_ok d_ok hb_bytes <<<"$line"
  if [ "${age:-999999}" -lt "$HB_MAXAGE" ] && [ "${ok:-0}" = 1 ] && [ "${m_ok:-0}" = 1 ] && [ "${d_ok:-0}" = 1 ]; then
    pass "heartbeat свежий (${age}s), ok, хранилище то ($((hb_bytes / 1048576)) МиБ)"
  else
    fail "heartbeat нездоров: age=${age:-?}s ok=${ok:-?} mount_ok=${m_ok:-?} device_ok=${d_ok:-?}"
  fi
  # Локальный файл ничего не доказывает, если он не доехал: сторож читает копию на мониторе.
  remote_size=$(printf 'ls %s\n' "$HB_REMOTE" | sftp -o BatchMode=yes -o ConnectTimeout=20 -b - "$SRC_ALIAS" 2>/dev/null | awk '{print $5}' | head -1)
  if [ -n "$remote_size" ] && [ "$remote_size" = "$(stat -c %s "$HB")" ]; then
    pass "копия heartbeat на мониторе совпадает по размеру ($remote_size Б)"
  else
    fail "копия heartbeat на мониторе не совпадает (монитор: ${remote_size:-нет}, локально: $(stat -c %s "$HB" 2>/dev/null || echo нет))"
  fi
else
  fail "нет $HB — репликация не отчитывалась"
fi

# --- 8. Репликация вообще запускается по расписанию -----------------------------
if crontab -l 2>/dev/null | grep -q 'BOTKIT_BACKLOG/stack/replicate.sh'; then
  pass "cron: $(crontab -l 2>/dev/null | grep 'BOTKIT_BACKLOG/stack/replicate.sh' | head -1)"
else
  fail "в crontab нет вызова replicate.sh — реплика не запускается сама"
fi

# --- 9. Полная проверка данных (дорого) ----------------------------------------
for repo in $REPOS; do
  if [ "$CHECK_READ_DATA" = 1 ]; then
    if "$R" -r "$S3/$repo" check --read-data >/dev/null 2>&1; then
      pass "$repo: check --read-data без ошибок"
    else
      fail "$repo: check --read-data нашёл ошибки"
    fi
  else
    note "$repo: check --read-data пропущен (запускайте с --full раз в квартал)"
  fi
done

echo "== итог: FAIL=$fails, не проверено=$unverified =="
if [ "$fails" -gt 0 ]; then exit 1; fi
if [ "$unverified" -gt 0 ]; then exit 2; fi
exit 0
