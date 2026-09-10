#!/bin/bash
# Собственный SQLite-snapshot (container python, crash-safe backup API) + export на deploy-уровень.
# docker cp пишет как клиент <- контейнер-демон; здесь клиент root -> chown/chmod для deploy.
set -u
EXPORT=/home/deploy/backups-export  # host-namespace for docker cp
LOGDIR=${DEST:-/home/deploy/backups-export}
BOTS="membership bookingbot reminder leadgen store support delivery docuflow pricesentry"
TS=$(date +%Y-%m-%d-%H%M)
for b in $BOTS; do
  mkdir -p "$EXPORT/$b"
  src="botkit-$b"
  snap="/app/backups/export.$TS.db"
  ok=1
  docker exec "$src" python3 -c "import sqlite3;src=sqlite3.connect('file:/app/data/bot.db?mode=ro',uri=True);dst=sqlite3.connect('$snap');src.backup(dst);dst.close();src.close()" || { echo "$b: backup FAILED"; ok=0; }
  [ $ok -eq 1 ] || continue
  docker cp "$src:$snap" "$EXPORT/$b/" || { echo "$b: cp FAILED"; continue; }
  docker exec "$src" rm -f "$snap"
  if [ "$(id -u)" = "0" ]; then
    chmod 644 "$EXPORT/$b/export.$TS.db"
    chown 1000:1000 "$EXPORT/$b/export.$TS.db"
  fi
  prune=$(ls -t "$EXPORT/$b/export."* 2>/dev/null | tail -n +40)
  [ -n "$prune" ] && rm -f $prune
done
echo "export done $TS:"