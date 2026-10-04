#!/usr/bin/env bash
# check_updates.sh — poll GHCR for new immutable tags on channel (v0.8.x) and
# auto-update bot containers via deploy_rollout.sh (health-gated, rollback).
# Channel comes from fleet.env (CHANNEL); expected tag = <CHANNEL>-<sha7> where
# sha7 is the latest *successful deploy.yml run* on main (GitHub API, public repos).
#
# Guards:
#   /root/botkit-maintenance        — touch → skip all (maintenance window)
#   /var/lib/botkit-rollout/attempted/<bot> — tag + date; skips retry <90 min
#     (prevents a rollback→retry→rollback loop on a persistently failing image)
# Log: /var/log/botkit-rollout-check.log
#
# Imported to version control 2026-10-03. Until then this file lived only at
# /opt/botkit-rollout/check_updates.sh with no copy in git, so no review could reach it
# and nothing noticed when it drifted. Two things changed on import: the channel is read
# from fleet.env instead of a hand-written /opt/botkit-rollout/version.conf, and the
# rollout script is resolved from this file's own directory instead of an absolute /opt
# path. The rollout logic itself is unchanged.
set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../lib/fleet.sh"

REGISTRY="ghcr.io"
OWNER="ninelegsdog"
STATE_DIR="/var/lib/botkit-rollout"
ATTEMPTED_DIR="$STATE_DIR/attempted"
MAINTENANCE_FLAG="/root/botkit-maintenance"
LOG="/var/log/botkit-rollout-check.log"
ROLLOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_rollout.sh"
RETRY_MINUTES=90

BOTS=(bookingbot leadgen store support membership pricesentry docuflow delivery reminder)

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) [check] $*" | tee -a "$LOG" >&2; }

# fleet.sh validates CHANNEL at load time and names it here if it is missing, so there is
# no second place to go wrong and no empty tag to poll for.
[[ -r "$ROLLOUT" ]] || {
  echo "$(now) [check] FATAL: rollout script not found next to me: $ROLLOUT" >&2
  exit 1
}

mkdir -p "$ATTEMPTED_DIR"

if [[ -f "$MAINTENANCE_FLAG" ]]; then
  log "maintenance flag present ($MAINTENANCE_FLAG) — skip all"
  exit 0
fi

latest_sha7() {
  # print latest successful deploy.yml run short sha, or empty
  local bot="$1"
  curl -sf --max-time 10 "https://api.github.com/repos/$OWNER/botkit-$bot/actions/runs?branch=main&per_page=30" \
    | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("workflow_runs", [])
except Exception:
    sys.exit(0)
for r in runs:
    if r.get("path", "").endswith("deploy.yml") and r.get("conclusion") == "success":
        print(r["head_sha"][:7])
        sys.exit(0)
sys.exit(0)
'
}

registry_digest() {
  # print sha256:... of the tag (index/manifest digest from Docker-Content-Digest), or empty if missing
  local bot="$1" tag="$2"
  local tok code
  tok=$(curl -sf --max-time 10 "https://$REGISTRY/token?scope=repository:$OWNER/botkit-$bot:pull" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))')
  [[ -z "$tok" ]] && { echo "tokfail"; return; }
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $tok" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json" \
    "https://$REGISTRY/v2/$OWNER/botkit-$bot/manifests/$tag")
  if [[ "$code" != "200" ]]; then
    echo "missing"; return
  fi
  # The digest is read from the response headers this writes to a file; capturing stdout
  # too was dead code (curl writes the body to /dev/null) and shellcheck was right about it.
  curl -s -D /tmp/check_h.$$ -o /dev/null --max-time 15 -H "Authorization: Bearer $tok" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json" \
    "https://$REGISTRY/v2/$OWNER/botkit-$bot/manifests/$tag"
  grep -i '^docker-content-digest:' /tmp/check_h.$$ 2>/dev/null | awk '{print $2}' | tr -d '\r'
  rm -f /tmp/check_h.$$
}

current_digest() {
  local bot="$1" img
  img=$(docker inspect -f '{{.Config.Image}}' "botkit-$bot" 2>/dev/null) || return
  docker image inspect -f '{{index .RepoDigests 0}}' "$img" 2>/dev/null | awk -F@ '{print $2}'
}

attempted_still_recent() {
  local bot="$1" tag="$2"
  local f="$ATTEMPTED_DIR/$bot"
  [[ -f "$f" ]] || return 1
  [[ "$(cat "$f")" == "$tag" ]] || return 1
  [[ -n "$(find "$f" -mmin -"$RETRY_MINUTES" -print -quit 2>/dev/null)" ]] && return 0
  return 1
}

rc_global=0
for bot in "${BOTS[@]}"; do
  sha7=$(latest_sha7 "$bot")
  if [[ -z "$sha7" ]]; then
    log "$bot: no successful deploy run yet — skip"; continue
  fi
  tag="$CHANNEL-$sha7"
  if attempted_still_recent "$bot" "$tag"; then
    # This used to be a bare `continue`, and that is how a total outage stayed invisible.
    # rc_global is only set on an actual rollout failure, so the tick after a failure skipped all
    # nine bots without setting anything and exited 0 - the unit went green while the fleet was
    # frozen on old images. The failure was visible for exactly one tick out of RETRY_MINUTES, and
    # on 03.10 that is how a ReadOnlyPaths mistake that stopped every rollout on the host read as
    # "mostly fine" on a dashboard. A bot stuck for 90 minutes is an incident, not noise.
    log "$bot: $tag attempted <${RETRY_MINUTES}m ago — still not rolled out (guard)"; rc_global=1; continue
  fi

  desired=$(registry_digest "$bot" "$tag")
  if [[ "$desired" == "missing" ]]; then
    log "$bot: $tag not in registry yet (CI still building) — skip"; continue
  fi
  if [[ "$desired" == "tokfail" ]]; then
    log "$bot: WARN registry token failed — skip tick"; rc_global=1; continue
  fi
  if [[ -z "$desired" ]]; then
    log "$bot: WARN no digest for $tag — skip tick"; rc_global=1; continue
  fi

  cur=$(current_digest "$bot")
  if [[ "$cur" == "$desired" ]]; then
    log "$bot: already current ($tag, ${desired:0:19}) — ok"; continue
  fi
  log "$bot: NEW $tag (digest ${desired:0:19}), current ${cur:-unknown} — rolling out"
  echo "$tag" > "$ATTEMPTED_DIR/$bot"
  # bash explicitly: repository files are not marked executable, the same reason the
  # systemd units name /usr/bin/bash rather than the script directly.
  /usr/bin/bash "$ROLLOUT" "$bot" "$tag" > /tmp/check_rc.$$ 2>&1; rc=$?
  sed "s/^/    /" /tmp/check_rc.$$ | tail -8 >> "$LOG" 2>&1 || true
  rm -f /tmp/check_rc.$$
  if [[ "$rc" == "0" ]]; then
    rm -f "$ATTEMPTED_DIR/$bot"
    log "$bot: rollout OK -> $tag"
  else
    log "$bot: rollout FAILED rc=$rc -> $tag (attempted guard, retry in ${RETRY_MINUTES}m)"
    rc_global=1
  fi
done

exit "$rc_global"