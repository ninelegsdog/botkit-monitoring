#!/usr/bin/env bash
# Репликация restic-репозиториев монитора в WORM-хранилище predator (MinIO, COMPLIANCE 90d)
set -euo pipefail
SECRETS=/home/algtro/.config/botkit-backlog
R=/home/algtro/.local/bin/restic
S3=s3:http://127.0.0.1:9000/botkit-backlog
LOG=/home/algtro/.local/state/botkit-backlog/replicate.log
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%FT%TZ; }
# число снапшотов в репозитории (JSON, а не число строк вывода)
count_snaps() {
  "$R" -r "$1" snapshots --json 2>/dev/null \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "?"
}

# один прогон за раз
exec 9>"$HOME/.local/state/botkit-backlog/replicate.lock"
flock -n 9 || { echo "$(ts) УЖЕ ВЫПОЛНЯЕТСЯ — пропуск" >>"$LOG"; exit 75; }

# shellcheck disable=SC1090  # путь к секретам задаётся вне репозитория
set -a; . "$SECRETS/minio.env"; set +a
export AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
export AWS_DEFAULT_REGION=us-east-1
export RESTIC_PASSWORD_FILE="$SECRETS/target.pw"

rc_total=0
for repo in data cp monitor; do
  SRC="sftp:botkit-backlog-src:/repo-$repo"
  TGT="$S3/$repo"
  srcpw="$SECRETS/$repo.pw"
  if [ ! -s "$srcpw" ]; then
    echo "$(ts) SKIP $repo: нет пароля источника ($srcpw)" | tee -a "$LOG"; rc_total=1; continue
  fi
  echo "$(ts) === $repo: copy ===" | tee -a "$LOG"
  if ! "$R" -r "$TGT" snapshots >/dev/null 2>&1; then
    if ! "$R" -r "$TGT" init >>"$LOG" 2>&1; then
      echo "$(ts) INIT FAILED $repo" | tee -a "$LOG"; rc_total=1; continue
    fi
  fi
  if "$R" -r "$TGT" --from-repo "$SRC" --from-password-file "$srcpw" copy >>"$LOG" 2>&1; then
    n=$(count_snaps "$TGT")
    echo "$(ts) OK $repo (снапшотов в WORM: $n)" | tee -a "$LOG"
  else
    echo "$(ts) COPY FAILED $repo" | tee -a "$LOG"; rc_total=1
  fi
done
exit $rc_total
