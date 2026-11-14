#!/usr/bin/env bash
# prune_images.sh — hold each bot's image history to a bounded size.
#
# Why this exists: nothing ever removed an image. Every merge builds a new immutable
# tag, the rollout pulls it, and the previous one stays forever - on 14.11 the host
# carried 375 images for nine bots (about 40 per repository, 47.8 GB) on a 119 GB
# disk, and only a manual `docker system prune` had ever reclaimed anything. The
# growth is invisible until HostDiskSpaceHigh fires at 85%, which is late: by then
# the next pull can fail mid-rollout, and a failed pull is how a bot ends up on a
# half-updated compose file.
#
# Policy: per repository keep the newest KEEP tags (10 by default) plus the image the
# container is actually running. Rollback in deploy_rollout.sh only ever returns to
# the image that was running when the attempt started, so ten generations is far
# more history than a rollback can need, and the running tag is kept explicitly
# because a rollback target must survive even if it is older than the window.
#
# Removal is by tag reference, never by image id: one id can carry several tags, and
# `docker image rm <id>` refuses in that case. Dropping the tag is enough - when the
# last tag of an image goes, docker deletes the image. A tag held by a stopped
# container is refused by docker; that is logged and counted, not fatal, and the
# next run retries it.
#
# Usage: prune_images.sh [--dry-run]
set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../lib/fleet.sh"

REGISTRY="ghcr.io"
OWNER="ninelegsdog"
KEEP="${BOTKIT_KEEP_IMAGES:-10}"
LOG="${BOTKIT_PRUNE_LOG:-/var/log/botkit-image-prune.log}"
TEXTFILE="${BOTKIT_PRUNE_TEXTFILE:-/var/lib/node-exporter-textfile/botkit_image_prune.prom}"

DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) $*" | tee -a "$LOG" >&2; }

[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "BOTKIT_KEEP_IMAGES must be a number, got '$KEEP'" >&2; exit 2; }

removed_total=0
failed_total=0
count_metrics=$(mktemp) || exit 1
trap 'rm -f "$count_metrics"' EXIT

prune_repo() {
  local bot="$1" repo="$2" running="$3"
  local tag created kept=0 removed=0 failed=0
  local -a order=()

  # Newest first. CreatedAt is "YYYY-MM-DD HH:MM:SS +0000 UTC", so fields 2 and 3
  # sort correctly as text; the tag is field 1 and never contains a space.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    order+=("${line%% *}")
  done < <(docker image ls "$repo" --format '{{.Tag}} {{.CreatedAt}}' 2>/dev/null | sort -k2,3 -r)

  if [ "${#order[@]}" -eq 0 ]; then
    log "$bot: no local images for $repo — skip"
    return 0
  fi

  for tag in "${order[@]}"; do
    # Dangling tags are the business of `docker image prune`, which knows when the
    # id is still referenced; removing them by tag here would be a no-op loop.
    [ "$tag" = "<none>" ] && continue
    ref="$repo:$tag"
    if [ "$ref" = "$running" ]; then
      continue
    fi
    if [ "$kept" -lt "$KEEP" ]; then
      kept=$((kept + 1))
      continue
    fi
    if [ "$DRY_RUN" = 1 ]; then
      log "$bot: would remove $ref"
      removed=$((removed + 1))
      continue
    fi
    if docker image rm "$ref" >/dev/null 2>&1; then
      removed=$((removed + 1))
    else
      # Held by a stopped container, or the daemon is busy. Retried next run.
      log "$bot: could not remove $ref (in use?)"
      failed=$((failed + 1))
    fi
  done

  if [ "$DRY_RUN" != 1 ]; then
    local count
    count=$(docker image ls "$repo" -q 2>/dev/null | wc -l | tr -d ' ')
    printf 'botkit_image_count{bot="%s"} %s\n' "$bot" "$count" >> "$count_metrics"
  fi
  log "$bot: kept ${kept} newest + running, removed ${removed}, failed ${failed}"
  removed_total=$((removed_total + removed))
  failed_total=$((failed_total + failed))
}

for pair in $FLEET; do
  bot="${pair%%:*}"
  repo="$REGISTRY/$OWNER/botkit-$bot"
  running=$(docker inspect -f '{{.Config.Image}}' "botkit-$bot" 2>/dev/null || echo "")
  if [ -z "$running" ]; then
    # Without the running reference the window could delete the rollback target.
    log "$bot: cannot read the running image — skip this repository"
    failed_total=$((failed_total + 1))
    continue
  fi
  prune_repo "$bot" "$repo" "$running"
done

if [ "$DRY_RUN" != 1 ]; then
  # Dangling layers from deleted tags; -f because there is nothing to confirm
  # interactively in a timer. Volumes are not touched.
  if pruned=$(docker image prune -f 2>&1 | tail -1); then
    log "dangling: $pruned"
  else
    log "dangling: prune failed"
    failed_total=$((failed_total + 1))
  fi
fi

tmp=$(mktemp "${TEXTFILE}.XXXXXX") && {
  {
    echo "# HELP botkit_image_prune_last_run_seconds Unix time of the last image retention run"
    echo "# TYPE botkit_image_prune_last_run_seconds gauge"
    echo "botkit_image_prune_last_run_seconds $(date +%s)"
    echo "# HELP botkit_image_prune_removed_total Tags removed in the last run"
    echo "# TYPE botkit_image_prune_removed_total gauge"
    echo "botkit_image_prune_removed_total $removed_total"
    echo "# HELP botkit_image_prune_failures Tags that could not be removed in the last run"
    echo "# TYPE botkit_image_prune_failures gauge"
    echo "botkit_image_prune_failures $failed_total"
    echo "# HELP botkit_image_count Local images per bot repository after the run"
    echo "# TYPE botkit_image_count gauge"
    cat "$count_metrics"
  } > "$tmp"
  # node-exporter is unprivileged: 600 under root's umask is an invisible metric, and
  # BotkitImagePruneStale is an absent() rule, so it would fire on a working timer.
  chmod 644 "$tmp"
  mv -f "$tmp" "$TEXTFILE"
}

log "total: removed ${removed_total}, failed ${failed_total}, keep window ${KEEP}"
exit 0
