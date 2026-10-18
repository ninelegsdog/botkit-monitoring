#!/usr/bin/env bash
# Вносит пароли исходных репозиториев (data/cp/monitor) из KeePass в ~/.config/botkit-backlog/
set -euo pipefail
SECRETS=/home/algtro/.config/botkit-backlog
mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
if [ ! -t 0 ]; then
  echo "ОШИБКА: нужен интерактивный терминал (запускайте напрямую в tty, не через пайп/heredoc)" >&2
  exit 2
fi
prompt() {
  local f="$SECRETS/$1.pw" pw
  printf 'Пароль репозитория "%s" из KeePass (ввод скрыт, 300 с): ' "$1" >&2
  IFS= read -r -s -t 300 pw || { echo >&2; echo "ОШИБКА: таймаут ввода для $1" >&2; exit 3; }
  printf '\n' >&2
  [ -n "$pw" ] || { echo "ОШИБКА: пустой пароль для $1" >&2; exit 3; }
  (umask 077; printf '%s' "$pw" > "$f")
  echo "  записано: $f" >&2
}
for r in data cp monitor; do
  if [ -s "$SECRETS/$r.pw" ]; then
    echo "$r.pw уже существует — пропуск (удалите файл, чтобы перезаписать)" >&2
  else
    prompt "$r"
  fi
done
echo "Готово. Файлы:" >&2
ls -l "$SECRETS" >&2
