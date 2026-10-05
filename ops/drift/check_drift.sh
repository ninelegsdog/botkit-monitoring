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

# Which repository paths end up inside the image, read out of the recipe rather than assumed.
# A COPY whose source is absolute or a named build stage brings nothing from the repository and
# is skipped; `COPY . .` means the whole tree, reported as the single prefix ".". Returns nothing
# when the file is missing or has no readable COPY, and the caller treats that as "cannot tell,
# assume everything" - a check that reports too much is recoverable, one that reports nothing is
# not.
dockerfile_repo_paths() {
  local file="$1"
  [ -r "$file" ] || return 0
  awk '
    /^[[:space:]]*#/ { next }
    toupper($1) == "COPY" {
      # Drop the instruction and any flags (--chown=..., --from=stage, --link), then walk the
      # remaining words. Everything before the last one is a source; the last is the destination.
      n = 0
      for (i = 2; i <= NF; i++) {
        if ($i ~ /^--/) continue
        n++
        arg[n] = $i
      }
      if (n < 2) next
      for (i = 1; i < n; i++) {
        src = arg[i]
        if (src ~ /^\//) continue          # absolute path inside the image, not from the repo
        if (src ~ /^\/\.\./) continue
        sub(/^\.\//, "", src)
        if (src == ".") { print "."; exit }
        print src
      }
    }
  ' "$file" | sort -u
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
  # The reference for image staleness is origin/main, not this clone's HEAD. The image is
  # built from the branch tip by deploy.yml; HEAD is only the deploy checkout, and when the
  # checkout lagged behind, a branch-tip image was reported as "older than HEAD" while the
  # checkout's own commits were listed as unbuilt - the message named the opposite of what
  # happened. Tracking (HEAD vs origin/main) stays a separate, honest line above.
  if [ -n "${head:-}" ] && [ -n "${origin:-}" ] && [ "$img" != "unknown" ]; then
    # The sha follows a colon, never a slash: the registry and the repository name
    # sit in front of it. With a slash in the pattern the substitution matched nothing,
    # so this whole comparison was dead code - the log line printed a stale sha next to
    # HEAD and the bot was still reported OK. ops/e2e/tests/test_drift_contract.py
    # fails on the old pattern, because a check that cannot fire looks identical to a
    # check that has nothing to report.
    imgsha=$(echo "$img" | sed -n "s#.*:$CHANNEL-\([0-9a-f]\{7\}\).*#\1#p")
    if [ -z "$imgsha" ]; then
      track+=("cannot read the build sha from the image tag: $img")
    elif [ "$imgsha" != "${origin:0:7}" ]; then
      # A sha mismatch on its own is not staleness. deploy.yml skips documentation and
      # test paths, so for those commits no image is ever built and the tag legitimately
      # trails HEAD. Only a change that would land inside the image counts.
      if ! git -c safe.directory="$d" -C "$d" cat-file -e "$imgsha^{commit}" 2>/dev/null; then
        track+=("image sha $imgsha is not in this clone, cannot verify what it built")
      else
        # Which files land inside the image is a property of that bot's Dockerfile, and the
        # bots disagree. Seven copy `pyproject.toml` and `src/`; botkit-reminder and
        # botkit-membership copy the whole repository with `COPY . .`. So there is no correct
        # global list - a hardcoded one is either wrong for two bots or useless for seven.
        #
        # A list written by hand is also what produced this false positive in the first place.
        # It named `deploy`, so the compose change that fixed the image pinning was reported as
        # an unbuilt runtime change on all nine bots, and the drift check went red for a change
        # that, for seven of them, could not have altered the image at all.
        image_paths=$(dockerfile_repo_paths "$d/Dockerfile")
        if [ -z "$image_paths" ]; then
          # Cannot read the recipe. Report everything rather than quietly declaring it fine.
          stale=$(git -c safe.directory="$d" -C "$d" diff --name-only "$imgsha..$origin" 2>/dev/null | tr '\n' ' ')
        else
          # The prefix comes straight from a COPY line, so it carries the slash the recipe wrote:
          # "src/". Appending "/*" to that yields "src//*", which matches nothing at all - the
          # condition looks right and never fires, which is the failure mode this whole file is
          # about. Trailing slashes are stripped first, then the match is exact-or-under.
          stale=$(git -c safe.directory="$d" -C "$d" diff --name-only "$imgsha..$origin" 2>/dev/null \
            | while IFS= read -r changed; do
                [ -n "$changed" ] || continue
                for prefix in $image_paths; do
                  if [ "$prefix" = "." ]; then
                    printf '%s ' "$changed"; break
                  fi
                  bare="${prefix%/}"
                  if [[ "$changed" == "$bare" || "$changed" == "$bare"/* ]]; then
                    printf '%s ' "$changed"; break
                  fi
                done
              done)
        fi
        if [ -n "$stale" ]; then
          track+=("image $imgsha is older than origin/main ${origin:0:7}, unbuilt: $stale")
        else
          log "IMG OK $bot (image $imgsha vs origin/main ${origin:0:7}: docs, tests and CI only)"
        fi
      fi
    fi
  fi

  # --- pinned tag: does the container match what the env file says, and does that tag exist? ---
  # IMAGE_TAG is the single source of truth for the image since 04.10: deploy_rollout.sh writes it,
  # compose reads it through --env-file, and the generated override is gone. Nothing reconciled the
  # file against the running container, though, and I spent this whole session checking that pairing
  # by hand on every command - nine times, because there was no check to do it.
  #
  # A hand-edited IMAGE_TAG is the failure this catches: compose would keep resolving the new value
  # while the container ran the old image, and the two would disagree silently until the next
  # restart. There was a live instance on 03.10 - a rollout to :main left the env file pinned to
  # v0.8.2-84010fa and the container on :main at the same time, and nothing noticed.
  pin=$(sed -n 's/^IMAGE_TAG=//p' "$ENV_ROOT/$bot.env" 2>/dev/null | tail -1)
  # Only a tag-form reference can be compared with a tag. A digest reference (repo@sha256:...)
  # has no tag, and "${img##*:}" on one yields a fragment of the hash, which would then be
  # reported as a mismatch against a pin that is perfectly correct.
  case "$img" in
    *@*|unknown) img_is_tag=0 ;;
    *)          img_is_tag=1 ;;
  esac
  if [ -z "$pin" ]; then
    critical+=("IMAGE_TAG not set in $ENV_ROOT/$bot.env - compose will refuse to start it")
  elif [ "$img_is_tag" = 1 ]; then
    running_tag="${img##*:}"
    if [ "$running_tag" != "$pin" ]; then
      critical+=("image tag $running_tag does not match IMAGE_TAG=$pin in the env file")
    fi
    # Existence in the registry, derived from the image the container actually runs rather than
    # from a registry constant, so it keeps working if the registry is ever changed. The compose
    # validator checks the *form* of the reference and cannot know whether the tag was ever
    # published: a typo passes validation and fails at the next `docker pull`, which is the worst
    # moment to find out. `manifest inspect` does not pull the image.
    if [ -z "${BOTKIT_SKIP_REGISTRY_CHECK:-}" ]; then
      repo="${img%:*}"
      if ! docker manifest inspect "$repo:$pin" >/dev/null 2>&1; then
        track+=("pinned tag $pin is not published in $repo - compose resolves it, docker pull would fail")
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

  # --- data layer: can the runtime user actually write both bind-mounts? (CRITICAL) ---
  # compose pins user: "1001:1001" and bind-mounts ../data and ../backups into the
  # container, while each host directory keeps whatever owner it happens to have. The
  # compose healthcheck probes /health and a Redis socket - it never reads SQLite and
  # never writes a snapshot - so a mismatch is invisible from the outside: on 03.10 a
  # fleet-wide `chown -R deploy:deploy` left all nine bots unable to open their database
  # and every one still reported healthy. Worse, a long-lived process keeps working on
  # the descriptor it opened before the change, so the damage stays latent until a
  # rollout recreates the container and the app dies on "unable to open database file".
  # The only honest signal is to ask the container itself, as the user it actually runs
  # as.
  #
  # Both mounts, because they fail independently: on 05.10 data/ was perfectly writable
  # while backups/ was still root:root 700, so every consistent snapshot inside
  # stage_consistent() failed for two days and this check - which only ever touched
  # /app/data - kept reporting clean.
  #
  # Since 05.10 the snapshot no longer travels through /app/backups at all: it is taken
  # into the container's tmpfs and streamed out with exec cat (ops/lib/snapshot.sh),
  # and the bots' own code never mentions the directory - the mount is legacy. The
  # probe stays anyway: both directories are still bind-mounts owned by whoever last
  # touched them, and a uid mismatch there is precisely the failure that hid for two
  # days.
  if [ "$running" = "true" ]; then
    cuid=$(docker inspect -f '{{.Config.User}}' "$ctr" 2>/dev/null || echo "?")
    for probe in /app/data /app/backups; do
      mnt=${probe#/app/}
      if ! docker exec "$ctr" sh -c "touch $probe/.driftprobe && rm -f $probe/.driftprobe" 2>/dev/null; then
        duid=$(stat -c %u "$d/$mnt" 2>/dev/null || echo "?")
        critical+=("$mnt/ not writable by container user (dir uid=$duid, container user=$cuid)")
      fi
    done
  fi

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