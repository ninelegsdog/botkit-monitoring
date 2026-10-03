#!/usr/bin/env bash
# check_drift.sh — сверка прод-деплоя 9 ботов с каноном (HEAD, image-tag sha,
# network, health, compose-валидация). Расхождение -> алерт BotkitDrift.
# Запускается systemd timer (каждые 15 мин) на ПРОДЕ как root.
#
# Семантика:
#   - tracking-mismatch (HEAD != origin/main ИЛИ image-tag-sha != HEAD-sha):
#     это окно rollout после пуша (auto-rollout ~15 мин). Алерт — только если
#     mismatch держится > GRACE_S (30 мин) — настоящий застрявший дрейф.
#   - критично (host-network, контейнер down, compose fail, /health не 200):
#     алерт сразу.
# Лог: /var/log/botkit-drift.log; троттлинг алертов 6ч на бота.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../lib/fleet.sh"


DEPLOY_ROOT=/home/deploy
ENV_ROOT=/usr/local/etc/botkit
# Resolved relative to this file rather than pinned to /opt/botkit-drift. The pinned path
# pointed at a hand-installed copy that was byte-identical to this repository's file - which
# is one deploy away from diverging, with nothing watching it. Same reasoning as the unit's
# ExecStart: the file that runs should be the file that is in version control.
VALIDATOR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/validate_compose.py"
# CHANNEL arrives from fleet.sh, which fails at load time if it is unset. It used to be
# read from a hand-written /opt/botkit-rollout/version.conf that lived outside git, with a
# hardcoded v0.8.2 fallback - so a missing marker silently turned the image-tag check into a
# comparison against a value nobody was maintaining.

STATE_DIR=/var/backups/botkit-drift
WATCH_DIR="$STATE_DIR/watch"
ALERTED_DIR="$STATE_DIR/alerted"
LOG=/var/log/botkit-drift.log
AM_URL="$ALERTMANAGER_ALERTS_URL"
THROTTLE=21600   # 6h между повторами алерта
GRACE_S=1800     # 30 мин окно rollout для tracking-mismatch

BOTS="$FLEET"   # S1: no inline fallback. A hardcoded default here is how two
                # sources of truth come to disagree silently. BASE_URL and the
                # monitoring addresses come from the same file via fleet.sh.

mkdir -p "$STATE_DIR" "$WATCH_DIR" "$ALERTED_DIR"
now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
now_s() { date +%s; }
log() { echo "$(now) $*" >> "$LOG"; }

send_alert() { # $1=b $2=sev $3=reason
  local bot="$1" sev="$2" reason="$3"
  # The throttle key carries the severity, and critical is never throttled.
  # One key per bot meant a still-open warning swallowed the alert that mattered:
  # /health 000 or a stopped container stayed inside the six-hour window and the
  # operator saw "ALERT throttled" instead of the outage. A throttle exists to
  # stop repeats, not to decide which failures are worth reporting.
  local last="$ALERTED_DIR/$bot.$sev" age
  if [ "$sev" != "critical" ] && [ -f "$last" ]; then
    age=$(( $(now_s) - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && { log "ALERT throttled $bot ($sev: $reason)"; return 0; }
  fi
  local payload
  payload="[{\"labels\":{\"alertname\":\"BotkitDrift\",\"severity\":\"$sev\",\"bot\":\"$bot\",\"service\":\"botkit-drift\"},\"annotations\":{\"summary\":\"drift $bot\",\"description\":\"${reason:0:200}\"}}]"
  curl -s -o /dev/null -X POST -d "$payload" -H "Content-Type: application/json" "$AM_URL" --max-time 5 \
    && { echo "$(now)" > "$last"; log "ALERT sent ($sev $bot): $reason"; } \
    || log "ALERT send FAILED ($bot): $reason"
}

rc_total=0
for entry in $BOTS; do
  bot="${entry%%:*}"
  port="${entry##*:}"
  d="$DEPLOY_ROOT/botkit-$bot"
  ctr="botkit-$bot"
  [ -d "$d" ] || { send_alert "$bot" critical "deploy dir missing: $d"; rc_total=1; continue; }
  critical=()
  track=()

  # --- HEAD vs origin/main (tracking) ---
  if ! git -c safe.directory="$d" -C "$d" fetch --quiet origin main 2>>"$LOG"; then
    track+=("git fetch failed")
  else
    head=$(git -c safe.directory="$d" -C "$d" rev-parse HEAD 2>/dev/null)
    origin=$(git -c safe.directory="$d" -C "$d" rev-parse origin/main 2>/dev/null)
    if [ -n "$head" ] && [ -n "$origin" ] && [ "$head" != "$origin" ]; then
      track+=("HEAD ${head:0:7} != origin/main ${origin:0:7}")
    elif [ -z "$origin" ]; then
      track+=("origin/main unresolvable")
    fi
  fi

  # --- network: bridge botkit_<b>, not host (CRITICAL) ---
  netjson=$(docker inspect "$ctr" -f '{{json .NetworkSettings.Networks}}' 2>/dev/null || echo "")
  if [ -z "$netjson" ]; then
    critical+=("container inspect failed")
  else
    if ! echo "$netjson" | grep -q "botkit_$bot"; then
      critical+=("NOT on botkit_$bot network")
    fi
    if [ "${netjson%%\:*}x" != "x" ] && echo "$netjson" | grep -q '"host"'; then
      critical+=("HOST network mode")
    fi
  fi

  # --- container running (CRITICAL) ---
  running=$(docker inspect -f '{{.State.Running}}' "$ctr" 2>/dev/null || echo "unknown")
  [ "$running" = "true" ] || critical+=("container not running ($running)")

  # --- image tag: channel prefix + no unbuilt runtime change (tracking) ---
  img=$(docker inspect -f '{{.Config.Image}}' "$ctr" 2>/dev/null || echo "unknown")
  case "$img" in
    *":$CHANNEL-"*) ;;
    *) track+=("image tag not on channel $CHANNEL: $img") ;;
  esac
  if [ -n "${head:-}" ] && [ "$img" != "unknown" ]; then
    # The sha follows a colon, never a slash: the registry and the repository name
    # sit in front of it. With a slash in the pattern the substitution matched nothing,
    # so this whole comparison was dead code - the log line printed a stale sha next to
    # HEAD and the bot was still reported OK. ops/e2e/tests/test_drift_contract.py
    # fails on the old pattern, because a check that cannot fire looks identical to a
    # check that has nothing to report.
    imgsha=$(echo "$img" | sed -n "s#.*:$CHANNEL-\([0-9a-f]\{7\}\).*#\1#p")
    if [ -z "$imgsha" ]; then
      track+=("cannot read the build sha from the image tag: $img")
    elif [ "$imgsha" != "${head:0:7}" ]; then
      # A sha mismatch on its own is not staleness. deploy.yml skips documentation and
      # test paths, so for those commits no image is ever built and the tag legitimately
      # trails HEAD. Only a change that would land inside the image counts.
      if ! git -c safe.directory="$d" -C "$d" cat-file -e "$imgsha^{commit}" 2>/dev/null; then
        track+=("image sha $imgsha is not in this clone, cannot verify what it built")
      else
        # What counts is what lands inside the image. The build recipe (Dockerfile) is in;
        # the CI workflow that runs the build is deliberately not. A live run on 03.10
        # settled this: the only commits between the running tag and HEAD were the two that
        # changed deploy.yml, so including it pinned all nine bots to a permanent warning
        # that no rollout could clear except a pointless rebuild of the whole fleet.
        stale=$(git -c safe.directory="$d" -C "$d" diff --name-only "$imgsha..$head" -- \
          bot.py src deploy pyproject.toml Dockerfile docker-compose.yml 2>/dev/null | tr '\n' ' ')
        if [ -n "$stale" ]; then
          track+=("image $imgsha is older than HEAD ${head:0:7}, unbuilt: $stale")
        else
          log "IMG OK $bot (image $imgsha vs HEAD ${head:0:7}: docs, tests and CI only)"
        fi
      fi
    fi
  fi

  # --- /health (CRITICAL) ---
  # BASE_URL carries no trailing colon in fleet.env, so the separator has to be here.
  # smoke_all.sh has always spelled it "$BASE:$port/health"; without the colon this URL
  # comes out as http://127.0.0.18081/health, curl answers 000, and a perfectly healthy
  # fleet reports nine criticals. The drift check had never been run against this fleet.env,
  # which is why nobody saw it: the copy on the host hardcoded the address instead.
  # curl already prints its own 000 when it cannot connect. With "|| echo 000" appended the
  # failure arrived twice and the alert read "/health=000000", which looks like a status
  # nobody sent. Assign the fallback instead of concatenating it.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$BASE_URL:$port/health") || code=000
  [ "$code" = "200" ] || critical+=("/health=$code")

  # --- compose validation (CRITICAL) ---
  if [ -f "$VALIDATOR" ] && [ -f "$d/deploy/compose.yml" ]; then
    if ! python3 "$VALIDATOR" "$d/deploy/compose.yml" --bot="$bot" >/dev/null 2>>"$LOG"; then
      critical+=("compose validation FAILED")
    fi
    if ! docker compose --env-file "$ENV_ROOT/$bot.env" -f "$d/deploy/compose.yml" config --quiet 2>>"$LOG"; then
      critical+=("docker compose config failed")
    fi
  else
    critical+=("validator/compose missing")
  fi

  # --- emit ---
  if [ ${#critical[@]} -gt 0 ]; then
    log "CRITICAL $bot: ${critical[*]} (img=$img)"
    send_alert "$bot" critical "${critical[*]}"
    rc_total=1
    continue
  fi

  if [ ${#track[@]} -gt 0 ]; then
    # grace window: track first observation; alert only if persists > GRACE_S
    wf="$WATCH_DIR/$bot"
    if [ ! -f "$wf" ]; then
      echo "$(now_s)" > "$wf"
      log "WATCH start $bot: ${track[*]}"
    else
      t0=$(cat "$wf")
      age=$(( $(now_s) - t0 ))
      if [ "$age" -gt "$GRACE_S" ]; then
        log "DRIFT $bot (${age}s > grace): ${track[*]} (img=$img head=${head:-na})"
        send_alert "$bot" warning "${track[*]}"
        rc_total=1
      else
        log "WATCH hold $bot (${age}s < grace): ${track[*]}"
      fi
    fi
  else
    rm -f "$WATCH_DIR/$bot"
    log "OK $bot (head=${head:-na} img=$img health=200)"
  fi
done

exit "$rc_total"