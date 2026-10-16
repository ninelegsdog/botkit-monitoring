#!/usr/bin/env bash
#
# install-webhook-check.sh — поставить рантайм-бандл webhook-проверки из канона
# туда, откуда его берёт systemd.
#
#   bash ops/e2e/install-webhook-check.sh [--check] [--repo DIR] [--dest DIR]
#
#     --check   ничего не писать: сообщить, совпадает ли установленное с каноном.
#               Ненулевой код, если нет (в том числе если бандла нет вовсе).
#               Этим режимом пользуется check_drift.sh.
#     --repo    корень чекаута канона (по умолчанию — этот репозиторий).
#     --dest    каталог рантайм-бандла (по умолчанию /root/botkit-webhook-check).
#
# Почему копия, а не ExecStart прямо из клона:
#   unit идёт с User=root, а клон /home/deploy/botkit-monitoring на запись открыт
#   пользователю deploy. Запускать root-ом скрипт из каталога, который может
#   переписать другая учётка, — это повышение привилегий: кто запишет туда файл,
#   тот получит root на следующем запуске таймера. Рантайм обязан жить в root-owned
#   каталоге, и install -m 0700 это и обеспечивает.
#
# Почему отдельный установщик, а не «скопировать руками»:
#   16.10.2026 прод-копия webhook_check.py разошлась с каноном на две версии, и
#   этого не заметил никто. sync-canon.sh синхронизировал клон и проверял маркеры
#   содержимого *клона* — то есть проверялось то, что обновляется само, и не
#   проверялось то, что не обновлялось никогда. Файл, живущий вне git, — это дрейф,
#   который обязательно появится; вопрос лишь в том, заметят ли его.
#
# fleet.env едет из канона намеренно. Прод-копия когда-то содержала только FLEET и
# WEBHOOK_*, и отсутствие в ней ALERTMANAGER_ALERTS_URL молча выключило алерты:
# webhook_check.py печатал «is not set in fleet.env; alert not sent» в лог, а
# снаружи это выглядело как «всё чисто». Канон — источник истины по адресам
# (шаг 4 sync-canon это утверждает маркером), поэтому из канона едет и конфиг.
set -euo pipefail

DEST=${WEBHOOK_CHECK_INSTALL_DIR:-/root/botkit-webhook-check}
REPO=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --dest)  DEST=${2:?--dest needs a directory}; shift 2 ;;
    --repo)  REPO=${2:?--repo needs a directory}; shift 2 ;;
    *) echo "usage: $0 [--check] [--repo DIR] [--dest DIR]" >&2; exit 2 ;;
  esac
done

if [ -z "$REPO" ]; then
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
SRC_E2E="$REPO/ops/e2e"
SRC_LIB="$REPO/ops/lib"

# Файлы, которые запускает unit, и конфиг, который читает webhook_check.py: его
# load_contract ищет fleet.env рядом со скриптом. Список — часть контракта с
# systemd/botkit-webhook-check.service (ExecStart и есть первый из них); тест
# ops/e2e/tests/test_webhook_install_contract.py падает, если они разъедутся.
SCRIPT_FILES=(webhook_check.sh webhook_check.py webhook_check_lib.py webhook_metrics.py)
CONF_FILE=fleet.env

# Сначала — полнота канона. Половина бандла хуже, чем ничего: скрипт, у которого
# нет библиотеки, падает на импорте, а «поставилось, но не целиком» в логе
# установщика неотличимо от успеха.
for f in "${SCRIPT_FILES[@]}"; do
  [ -f "$SRC_E2E/$f" ] || { echo "FATAL: canon is missing ops/e2e/$f" >&2; exit 1; }
done
[ -f "$SRC_LIB/fleet.env" ] || { echo "FATAL: canon is missing ops/lib/fleet.env" >&2; exit 1; }

sha_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

drift=0
install_one() { # $1=source file, $2=name in DEST
  local src="$1" name="$2" mode
  # unit идёт root-ом: посторонний не должен даже читать код, исполняемый с такими правами.
  mode=$(case "$name" in "$CONF_FILE") echo 0600 ;; *) echo 0700 ;; esac)
  if [ -f "$DEST/$name" ] && [ "$(sha_of "$src")" = "$(sha_of "$DEST/$name")" ]; then
    echo "  ok        $name"
    return 0
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    if [ -f "$DEST/$name" ]; then
      echo "  DRIFT     $name"
    else
      echo "  MISSING   $name"
    fi
    drift=1
    return 0
  fi
  # Атомарная замена: пишем в $DEST/.new на той же ФС и переименовываем. Прямая
  # запись в цель оставила бы окно, в которое таймер успел бы запустить
  # наполовину записанный скрипт.
  install -m "$mode" "$src" "$DEST/.$name.new"
  mv -f "$DEST/.$name.new" "$DEST/$name"
  if [ "$(id -u)" = 0 ]; then
    chown root:root "$DEST/$name"
  fi
  echo "  installed $name ($mode)"
  return 0
}

if [ ! -d "$DEST" ]; then
  if [ "$CHECK_ONLY" = 1 ]; then
    echo "  MISSING   $DEST (no bundle installed at all)"
    exit 1
  fi
  install -d -m 0700 "$DEST"
  if [ "$(id -u)" = 0 ]; then
    chown root:root "$DEST"
  fi
fi

echo "install-webhook-check: canon=$REPO dest=$DEST check_only=$CHECK_ONLY"
for f in "${SCRIPT_FILES[@]}"; do
  install_one "$SRC_E2E/$f" "$f"
done
install_one "$SRC_LIB/fleet.env" fleet.env

if [ "$CHECK_ONLY" = 1 ]; then
  [ "$drift" -eq 0 ] || { echo "install-webhook-check: DRIFT detected" >&2; exit 1; }
  echo "install-webhook-check: installed bundle matches canon"
  exit 0
fi

# Доказательство с другой стороны: после записи сверяем хэши цели с источником, а
# не доверяем успешному exit кода install. Тавтологию («мы только что это
# скопировали») держать тут нечего — сверка ловит и обрезанную запись, и чужой
# процесс, переписавший файл между install и проверкой.
for f in "${SCRIPT_FILES[@]}"; do
  if [ "$(sha_of "$SRC_E2E/$f")" != "$(sha_of "$DEST/$f")" ]; then
    echo "FATAL: $DEST/$f does not match canon after install" >&2
    exit 1
  fi
done
if [ "$(sha_of "$SRC_LIB/fleet.env")" != "$(sha_of "$DEST/fleet.env")" ]; then
  echo "FATAL: $DEST/fleet.env does not match canon after install" >&2
  exit 1
fi
echo "install-webhook-check: verified — installed bundle matches canon"
