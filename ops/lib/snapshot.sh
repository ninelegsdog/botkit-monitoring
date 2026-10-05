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
#   2. перенос на хост через `docker exec ... cat`.
#      ВАЖНО: `docker cp` здесь использовать нельзя. Проверено на проде 05.10:
#      файл в /tmp контейнера есть (exec его видит), а docker cp отвечает
#      "Could not find the file" - daemon архивирует слои образа, а не
#      mount-namespace работающего контейнера, и содержимое tmpfs в них нет;
#   3. если контейнер не запущен или docker не отвечает - тот же online-backup API
#      средствами хостового python3 (бэкап остановленного бота всё равно нужен);
#   4. иначе отказ. `cp` намеренно не остаётся в качестве запасного варианта:
#      надорванная копия хуже её отсутствия - её нельзя отличить от настоящей.
#
# Файл проверяется дважды: размер больше нуля (пустой файл проходит
# PRAGMA integrity_check и выглядел бы как успех) и сам integrity_check. Не прошедший
# проверку файл удаляется, функция возвращает отказ.
#
# consistent_snapshot <контейнер> <база внутри контейнера>
#                     <база на хосте> <куда положить>
#   контейнер пустой или не запущен - сразу берётся хостовой путь.
consistent_snapshot() {
  local ctr="$1" src_in_ctr="$2" src_on_host="$3" dest="$4"
  local tmp="/tmp/botkit-snap.$$.db" ok=0

  # Источник обязан быть непустым до всяких попыток: PRAGMA integrity_check на
  # нулевом файле отвечает "ok" (sqlite принимает его за пустую базу), поэтому
  # бот с пропавшим bot.db иначе получил бы «успешный» бэкап из ничего.
  if [ ! -s "$src_on_host" ]; then
    echo "snapshot: источник $src_on_host отсутствует или пуст" >&2
    return 1
  fi

  if [ -n "$ctr" ] && docker inspect "$ctr" >/dev/null 2>&1; then
    if docker exec "$ctr" python3 -c "
import sqlite3
src = sqlite3.connect('file:$src_in_ctr?mode=ro', uri=True)
dst = sqlite3.connect('$tmp')
src.backup(dst)
dst.close(); src.close()" 2>/dev/null; then
      if docker exec "$ctr" cat "$tmp" > "$dest" 2>/dev/null && [ -s "$dest" ]; then
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

  if [ "$ok" -ne 1 ] || [ ! -s "$dest" ]; then
    rm -f "$dest"
    echo "snapshot: $src_on_host - согласованная копия не удалась" >&2
    return 1
  fi

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
  chmod 600 "$dest"
  return 0
}
