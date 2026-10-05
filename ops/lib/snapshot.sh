#!/usr/bin/env bash
# Единственный способ получить согласованную копию SQLite в этом репозитории.
#
# До 05.10.2026 у двух потоков была своя логика, и у каждой — свой способ сломаться:
#   - restic-backup (stage_consistent) писал снимок в /app/backups контейнера, то есть
#     в bind-mount хоста. Хостовой каталог остался root:root 700 после общего
#     chown -R 03.10, контейнер работает от 1001:1001 - и все девять согласованных
#     снимков тихо перестали создаваться на два дня, пока все проверки зеленели;
#   - backup_bots брал обычный `cp` с хоста: копия SQLite, снятая в момент записи,
#     бывает надорванной, и integrity_check её не всегда ловит.
#
# Порядок здесь один и общий для обоих потоков:
#   1. снимок через online-backup API внутри контейнера, во временный файл в /tmp
#      контейнера. Там tmpfs (объявлен при read_only: true), то есть владение
#      хостовым каталогом на результат вообще не влияет;
#   2. docker cp оттуда на хост;
#   3. если контейнер не запущен или docker не отвечает - тот же online-backup API
#      средствами хостового python3 (бэкап остановленного бота всё равно нужен);
#   4. иначе отказ. `cp` намеренно не остаётся в качестве запасного варианта:
#      надорванная копия хуже её отсутствия - её нельзя отличить от настоящей.
#
# После копии файл всегда проверяется PRAGMA integrity_check: снимок, который не
# проходит проверку, удаляется, а функция возвращает отказ.
#
# consistent_snapshot <контейнер или путь к базе-источнику на хосте>
#                     <база внутри контейнера> <база на хосте> <куда положить>
#   контейнер пустой - сразу берётся хостовой путь (например, docker недоступен).
consistent_snapshot() {
  local ctr="$1" src_in_ctr="$2" src_on_host="$3" dest="$4"
  local tmp="/tmp/botkit-snap.$$.db" ok=0

  if [ -n "$ctr" ] && docker inspect "$ctr" >/dev/null 2>&1; then
    if docker exec "$ctr" python3 -c "
import sqlite3
src = sqlite3.connect('file:$src_in_ctr?mode=ro', uri=True)
dst = sqlite3.connect('$tmp')
src.backup(dst)
dst.close(); src.close()" 2>/dev/null; then
      if docker cp "$ctr:$tmp" "$dest" >/dev/null 2>&1; then
        ok=1
      fi
    fi
    docker exec "$ctr" rm -f "$tmp" >/dev/null 2>&1 || true
    [ "$ok" -eq 1 ] || echo "snapshot: $ctr - путь через tmpfs не удался, пробую хост" >&2
  fi

  if [ "$ok" -ne 1 ]; then
    rm -f "$dest"
    if python3 - "$src_on_host" "$dest" <<'PY' 2>/dev/null
import sqlite3, sys
src = sqlite3.connect('file:%s?mode=ro' % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
    then
      ok=1
    fi
  fi

  [ "$ok" -eq 1 ] || { rm -f "$dest"; return 1; }

  if ! python3 - "$dest" <<'PY' 2>/dev/null
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("PRAGMA integrity_check").fetchall()
c.close()
sys.exit(0 if r == [('ok',)] else 1)
PY
  then
    echo "snapshot: $dest не прошёл integrity_check" >&2
    rm -f "$dest"
    return 1
  fi
  return 0
}
