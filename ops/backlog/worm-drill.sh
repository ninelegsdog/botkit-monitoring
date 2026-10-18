#!/usr/bin/env bash
# WORM-дрилл: доказательство неизменяемости реплики бэкапов.
#
# Атака `forget --keep-last 1 --prune` с резервного ключа обязана не стереть
# физически ни одного объекта внутри срока COMPLIANCE: её удаления — только
# delete-маркеры. Снятие маркеров возвращает полную историю, restore цели
# атаки снова работает.
#
# Использование: bash worm-drill.sh [репо]     (репо по умолчанию: data)
# Запускать вручную (квартально) при остановленной репликации.
set -euo pipefail
REPO="${1:-data}"
SECRETS=/home/algtro/.config/botkit-backlog
R=/home/algtro/.local/bin/restic
S3=s3:http://127.0.0.1:9000/botkit-backlog
PY=/home/algtro/.local/venvs/s3admin/bin/python

# shellcheck disable=SC1090  # путь к секретам задаётся вне репозитория
set -a; . "$SECRETS/minio.env"; set +a
export AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
export AWS_DEFAULT_REGION=us-east-1
export RESTIC_PASSWORD_FILE="$SECRETS/target.pw"

count()     { "$R" -r "$S3/$REPO" snapshots --json 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'; }
oldest()    { "$R" -r "$S3/$REPO" snapshots --json 2>/dev/null | python3 -c 'import json,sys; s=json.load(sys.stdin); print(sorted(s,key=lambda x:x["time"])[0]["short_id"])'; }
unlock_all() { "$R" -r "$S3/$REPO" unlock --remove-all >/dev/null 2>&1 || true; }

# delete-маркеры под репо/ (кроме /locks/: их маркеры прячут локи — снимать нельзя)
s3_py() { "$PY" - "$REPO" "$1" <<'PY'
import os, sys, boto3
repo, mode = sys.argv[1], sys.argv[2]
s3 = boto3.client('s3', endpoint_url='http://127.0.0.1:9000',
    aws_access_key_id=os.environ['MINIO_ROOT_USER'],
    aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'], region_name='us-east-1')
def marks():
    out = []
    for page in s3.get_paginator('list_object_versions').paginate(Bucket='botkit-backlog', Prefix=repo + '/'):
        out += [m for m in page.get('DeleteMarkers', []) if '/locks/' not in m['Key']]
    return out
if mode == 'count':
    print(len(marks()))
else:
    n = 0
    for _ in range(200):
        b = marks()
        if not b: break
        for m in b:
            s3.delete_object(Bucket='botkit-backlog', Key=m['Key'], VersionId=m['VersionId']); n += 1
    print(n)
PY
}
markers()      { s3_py count; }
clean_markers(){ s3_py clean; }

fail=0
chk() { if [ "$2" = "$3" ]; then echo "  ✅ $1: $2"; else echo "  ❌ $1: получено $2, ожидалось $3"; fail=1; fi; }

unlock_all
N0=$(count); OLD=$(oldest)
echo "=== ДО: снапшотов=$N0, цель атаки (самый старый)=$OLD ==="

echo "=== АТАКА: forget --keep-last 1 --prune --group-by '' ==="
# --group-by "" — атака сильнее штатной: без группировки по host+paths видимым
# остаётся ровно один снапшот (иначе forget прячет по одному на каждую группу).
if "$R" -r "$S3/$REPO" forget --keep-last 1 --prune --group-by "" >/tmp/worm-drill-attack.out 2>&1; then ARC=0; else ARC=$?; fi
echo "  атака rc=$ARC; хвост вывода: $(tail -1 /tmp/worm-drill-attack.out)"
unlock_all
N1=$(count); M1=$(markers)
echo "  после атаки: видимых снапшотов=$N1, delete-маркеров=$M1"

echo "=== A. объекты физически целы (удаления = маркеры) ==="
chk "видимый срез сузился до 1" "$N1" "1"
if [ "$M1" -gt 0 ]; then echo "  ✅ маркеров=$M1 (удаления обратимы)"; else echo "  ❌ маркеров нет"; fail=1; fi

echo "=== B. эффект атаки реален: цель недоступна ==="
rm -rf /tmp/worm-drill-restore
if "$R" -r "$S3/$REPO" restore "$OLD" --target /tmp/worm-drill-restore >/tmp/worm-drill-restore.out 2>&1; then RRC=0; else RRC=$?; fi
if [ "$RRC" -ne 0 ]; then echo "  ✅ restore спрятанного снапшота rc=$RRC (ожидаемый отказ)"; else echo "  ❌ спрятанный снапшот всё ещё восстанавливается"; fail=1; fi

echo "=== C. обратимость: снятие маркеров ==="
CLEANED=$(clean_markers)
N2=$(count); M2=$(markers)
echo "  снято маркеров=$CLEANED; видимых снапшотов=$N2; осталось маркеров=$M2"
chk "история восстановлена" "$N2" "$N0"
rm -rf /tmp/worm-drill-restore
if "$R" -r "$S3/$REPO" restore "$OLD" --target /tmp/worm-drill-restore >/tmp/worm-drill-restore.out 2>&1; then RRC2=0; else RRC2=$?; fi
chk "restore цели атаки снова работает (rc)" "$RRC2" "0"
rm -rf /tmp/worm-drill-restore /tmp/worm-drill-attack.out /tmp/worm-drill-restore.out

echo "=== ИТОГ: $([ "$fail" = 0 ] && echo 'PASS — данные под COMPLIANCE не утрачиваются, удаления обратимы' || echo 'FAIL') ==="
exit $fail
