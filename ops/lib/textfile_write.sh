#!/usr/bin/env bash
# textfile_write.sh — атомарная запись textfile-метрики с корректными правами.
#
# Паттерн mktemp + mv атомарен, но mktemp создаёт файл с mode 0600, а mv
# сохраняет mode. node-exporter работает непривилегированным пользователем,
# поэтому 0600 делает метрики невидимыми → absent()-алерты срабатывают ложно.
#
# Использование:
#   source ops/lib/textfile_write.sh
#   write_textfile "/var/lib/node-exporter-textfile/foo.prom" <<'EOF'
#   metric_name 1
#   EOF
#
# Или:
#   write_textfile "/path/to/file.prom" "content string"
#
# Функция: mktemp → write → chmod 644 → mv. При любой ошибке — cleanup temp.
set -u

write_textfile() {
  local target="$1"
  local content="${2:-}"
  local tmp

  tmp=$(mktemp "${target}.XXXXXX") || { echo "textfile_write: mktemp failed for $target" >&2; return 1; }

  # cleanup on exit
  trap 'rm -f "$tmp"' EXIT HUP INT TERM

  if [ -n "$content" ]; then
    printf '%s\n' "$content" > "$tmp" || { rm -f "$tmp"; trap - EXIT HUP INT TERM; return 1; }
  else
    # read from stdin
    cat > "$tmp" || { rm -f "$tmp"; trap - EXIT HUP INT TERM; return 1; }
  fi

  chmod 644 "$tmp" || { rm -f "$tmp"; trap - EXIT HUP INT TERM; return 1; }
  mv -f "$tmp" "$target" || { rm -f "$tmp"; trap - EXIT HUP INT TERM; return 1; }

  trap - EXIT HUP INT TERM
  return 0
}
