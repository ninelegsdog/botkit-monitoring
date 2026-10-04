#!/usr/bin/env bash
# verify_rollout.sh — proves deploy_rollout.sh can do its job, before the timer uses it.
#
# Two things are checked, because a rollout that cannot start is silent by nature: it aborts
# before it touches anything, and the container it was meant to update stays on the old image
# looking perfectly healthy.
#
# 1. Structure: run the script in --dry-run and look for a shell error in what it printed.
#    `bash -n` cannot cover this class. Bash defines a function when it *executes* the
#    definition, so a function written inside another function's body is valid syntax and does
#    not exist at run time. On 03.10 a misplaced brace did exactly that: compose_up() swallowed
#    send_alert, data_preflight and the whole dry-run block. 216 tests passed, bash -n passed,
#    and it surfaced only when the script ran on the live host.
#
# 2. Writability: for every bot, actually write the tag that is already in the file.
#    This is the check that would have caught tonight's outage. The pin lives in
#    /usr/local/etc/botkit/<bot>.env, and that directory was in the unit's ReadOnlyPaths, so
#    set_image_tag failed for all nine bots. Every rollout aborted after the pull and before
#    touching the container, so the fleet stayed healthy and simply stopped updating - and the
#    unit turned green again 15 minutes later when the retry guard skipped the bots without
#    setting a failure code.
#
#    Rewriting the same value is a no-op in content and not a no-op in effect: it exercises the
#    exact function, the exact directory and this unit's own sandbox, because ExecStartPre
#    inherits the unit's filesystem restrictions. Nothing else in the stack has that property.
#
# Deliberately not fatal: a non-zero exit from --dry-run with no shell error in the output.
# That is the environment, not the script - no compose file next door, for instance. Blocking
# rollouts because a check cannot find its neighbour turns into silent permanent staleness
# after about a month, and nobody notices. Only a broken script stops the unit.
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROLLOUT="$HERE/deploy_rollout.sh"
LOG=/var/log/botkit-rollout.log
ENV_ROOT=/usr/local/etc/botkit

# The bot list lives in check_updates.sh, which is the only place that has to know it. Reading it
# from there means this check cannot drift into testing a bot that no longer exists.
BOTS=$(sed -n 's/^BOTS=(\(.*\))$/\1/p' "$HERE/check_updates.sh" | tr -d '"' | tr ' ' '\n' | grep -v '^$')
if [[ -z "$BOTS" ]]; then
  echo "verify: FATAL cannot read the bot list out of check_updates.sh" >&2
  exit 1
fi

broken=0

# --- 1. structure ---------------------------------------------------------------------------------
DRY_TAG="v0.8.2-0000000"   # never pulled: --dry-run exits before docker pull
first_bot=$(echo "$BOTS" | head -1)
out=$(/usr/bin/bash "$ROLLOUT" "$first_bot" "$DRY_TAG" --dry-run 2>&1)
rc=$?
if grep -qE 'command not found|unbound variable|syntax error|: line [0-9]+:' <<<"$out"; then
  {
    echo "verify: FATAL $ROLLOUT reported a shell error while planning"
    echo "$out" | sed 's/^/  /'
    echo "verify: refusing to let the timer use it"
  } | tee -a "$LOG" >&2
  broken=1
elif [[ "$rc" -ne 0 ]]; then
  {
    echo "verify: WARN --dry-run exited $rc for $first_bot:$DRY_TAG (no shell error - environment, not breakage)"
    echo "$out" | sed 's/^/  /'
  } | tee -a "$LOG" >&2
fi

# --- 2. can the rollout write the pin? -------------------------------------------------------------
# Done through the rollout's own function rather than a hand-rolled `touch`, because the thing
# that broke was that function's write, not the directory's existence. Sourcing the script runs
# its top level, so instead the function is extracted and evaluated on its own - it only needs
# $ENV_FILE, $LOG and log(), all of which the caller sets below.
set_image_tag_body=$(sed -n '/^set_image_tag() {/,/^}/p' "$ROLLOUT")
if [[ -z "$set_image_tag_body" ]]; then
  echo "verify: FATAL set_image_tag() is not defined in $ROLLOUT" >&2
  exit 1
fi
# A partial extraction would eval into something that quietly does nothing and reports success,
# which is the failure mode this script exists to catch. Require the operations that make the
# write real.
for needle in 'mktemp' 'IMAGE_TAG=' 'cat "$tmp" >"$ENV_FILE"'; do
  case "$set_image_tag_body" in
    *"$needle"*) ;;
    *) echo "verify: FATAL extracted set_image_tag() is missing '$needle' - extraction is partial" >&2; exit 1 ;;
  esac
done

# set_image_tag calls log() on the rejected-tag path, so the scope has to provide it. It also
# logs $bot, which is meaningless here; the label keeps the line readable.
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) verify $*" | tee -a "$LOG" >&2; }
bot="verify"

write_failures=0
checked=0
for bot in $BOTS; do
  envf="$ENV_ROOT/$bot.env"
  if [[ ! -f "$envf" ]]; then
    echo "verify: WARN no env file for $bot ($envf) - skipping its write check" | tee -a "$LOG" >&2
    continue
  fi
  current=$(sed -n 's/^IMAGE_TAG=//p' "$envf" | tail -1)
  if [[ -z "$current" ]]; then
    echo "verify: WARN $bot.env has no IMAGE_TAG - the pin is not seeded, cannot prove the write" \
      | tee -a "$LOG" >&2
    continue
  fi

  # shellcheck disable=SC2034  # read by the eval'd set_image_tag body below, invisible to shellcheck
  ENV_FILE="$envf"
  # The function is eval'd rather than sourced: sourcing deploy_rollout.sh runs its top level, and
  # the --dry-run branch calls `exit`, which would take this script down with it. shellcheck cannot
  # see into the eval either, hence the disable on both counts.
  # shellcheck disable=SC1090
  if ! eval "$set_image_tag_body '$current'"; then
    {
      echo "verify: FATAL $bot: set_image_tag cannot write $envf (IMAGE_TAG=$current)"
      echo "         the rollout would abort before recreating the container, for every bot"
    } | tee -a "$LOG" >&2
    write_failures=$((write_failures + 1))
    continue
  fi

  # Content must be byte-identical: this check is allowed to prove writability, never to change
  # the deployment. A mismatch here means the function rewrote more than the tag.
  after=$(sed -n 's/^IMAGE_TAG=//p' "$envf" | tail -1)
  if [[ "$after" != "$current" ]]; then
    echo "verify: FATAL $bot: IMAGE_TAG changed from '$current' to '$after' during a no-op write" \
      | tee -a "$LOG" >&2
    write_failures=$((write_failures + 1))
    continue
  fi
  checked=$((checked + 1))
done

if [[ "$write_failures" -gt 0 ]]; then
  {
    echo "verify: FATAL $write_failures of $(echo "$BOTS" | wc -w) bots cannot have their image pin written"
    echo "verify: refusing to let the timer use it"
  } | tee -a "$LOG" >&2
  broken=1
fi

if [[ "$broken" -eq 0 ]]; then
  echo "verify: ok $ROLLOUT plans without touching anything, and the image pin is writable for $checked bots" \
    | tee -a "$LOG"
fi

exit "$broken"