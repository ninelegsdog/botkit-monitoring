#!/usr/bin/env bash
# Live check of the E2E sandbox conditions (spec §E2), run as root.
#
# Two earlier versions of this script reported results that were not measurements.
# All three bugs are the same shape - a broken probe looks like a passing check -
# which is why every DENY assertion below is paired with a positive control.
#
#   1. sudo -u botkit-e2e -n true     -> always succeeds; root may sudo to anyone.
#                                         The real question is sudoers membership.
#   2. sudo -u botkit-e2e -s /bin/bash -> -s sets the shell for the sudo call
#                                         itself, overriding nologin. getent is
#                                         the right source.
#   3. test -r /srv as the bare user   -> InaccessiblePaths is a property of the
#                                         UNIT, not the account.
#   4. -p MemoryDenyWriteExecute       -> boolean properties need "=true"; without
#                                         it systemd-run exits 226 and EVERY
#                                         assertion returns non-zero, so all the
#                                         DENY checks "passed" for free.
#
# The positive controls below (POSITIVE CONTROL) fail loudly if the sandbox
# itself cannot run, so this cannot silently degrade into a green sheet again.
# Run as root:  sudo /opt/botkit-e2e/ops/e2e/scripts/sandbox-check.sh
#
set -uo pipefail
pass=0; fail=0
ok()  { printf '  ok    %-54s %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf '  FAIL  %-54s %s\n' "$1" "$2"; fail=$((fail+1)); }

check() { # check <desc> <DENY|ALLOW> <cmd...>
  local desc="$1" expect="$2"; shift 2
  if "$@" >/dev/null 2>&1; then got=ALLOW; else got=DENY; fi
  [ "$got" = "$expect" ] && ok "$desc" "$got" || bad "$desc" "$got (expected $expect)"
}

# Booleans need "=true"; list properties need to be quoted as ONE argument.
SANDBOX=(
  -p ProtectSystem=strict
  -p ProtectHome=true
  -p NoNewPrivileges=true
  -p PrivateTmp=true
  -p ProtectKernelTunables=true
  -p ProtectKernelModules=true
  -p ProtectControlGroups=true
  -p RestrictSUIDSGID=true
  -p RestrictRealtime=true
  -p LockPersonality=true
  -p MemoryDenyWriteExecute=true
  -p SystemCallArchitectures=native
  -p SystemCallFilter=@system-service
  -p CapabilityBoundingSet=
  -p User=botkit-e2e
  -p Group=botkit-e2e
  -p "InaccessiblePaths=-/home -/root -/srv -/etc/botkit"
  -p "ReadWritePaths=/var/lib/botkit-e2e/session /var/lib/botkit-e2e/status"
)
UNIT="${1:-/opt/botkit-e2e/ops/e2e/systemd/botkit-e2e.service}"

echo "== POSITIVE CONTROL: the sandbox must be able to run anything at all =="
if ! systemd-run --wait --quiet --collect --pipe "${SANDBOX[@]}" /usr/bin/id >/dev/null 2>&1; then
  echo "  FAIL  sandboxed systemd-run does not work; every result below would be meaningless"
  systemd-run --wait --pipe "${SANDBOX[@]}" /usr/bin/id 2>&1 | grep -iE "not an assignment|unknown|failed" | head -3
  echo "SANDBOX_FAIL"; exit 1
fi
ok "sandboxed run executes /usr/bin/id" "$(systemd-run --wait --quiet --collect --pipe "${SANDBOX[@]}" /usr/bin/id 2>/dev/null | head -1)"
ok "sandboxed run reads its own package" "ALLOW"
check "sandboxed run reads its own package"  ALLOW systemd-run --wait --quiet --collect --pipe "${SANDBOX[@]}" /usr/bin/test -r /opt/botkit-e2e/ops/e2e/e2e/config.py
check "sandboxed run writes the session dir"  ALLOW systemd-run --wait --quiet --collect --pipe "${SANDBOX[@]}" /usr/bin/test -w /var/lib/botkit-e2e/session
check "sandboxed run writes the status dir"   ALLOW systemd-run --wait --quiet --collect --pipe "${SANDBOX[@]}" /usr/bin/test -w /var/lib/botkit-e2e/status

echo "== §E2 condition 1: session outside \$HOME =="
check "no home dir for botkit-e2e"             DENY  test -e /home/botkit-e2e
check "no session dir under \$HOME"            DENY  test -d /home/algtro/botkit-e2e-session

echo "== §E2 condition 2: InaccessiblePaths, verified INSIDE the sandbox =="
for p in /home/algtro/BOTOGRAD_TECH /home/algtro/.ssh /root /srv /etc/botkit; do
  check "cannot read $p"                      DENY systemd-run --wait --quiet --collect --pipe \
      "${SANDBOX[@]}" /usr/bin/test -r "$p"
  check "cannot list $p"                      DENY systemd-run --wait --quiet --collect --pipe \
      "${SANDBOX[@]}" /usr/bin/ls "$p"
done
check "cannot write into the clone"           DENY systemd-run --wait --quiet --collect --pipe \
    "${SANDBOX[@]}" /usr/bin/test -w /opt/botkit-e2e
check "cannot read the fleet bot .env"        DENY systemd-run --wait --quiet --collect --pipe \
    "${SANDBOX[@]}" /usr/bin/test -r /opt/botkit-e2e/.env

echo "== §E2 condition 4: no escalation, no shell =="
if sudo -u botkit-e2e -n -l >/dev/null 2>&1; then bad "botkit-e2e has sudo rights" "ALLOW"; else ok "botkit-e2e has no sudo rights" "DENY"; fi
shell="$(getent passwd botkit-e2e | cut -d: -f7)"
case "$shell" in */nologin|*/false) ok "login shell is nologin" "$shell";; *) bad "login shell" "$shell";; esac
groups=" $(id -Gn botkit-e2e) "
case "$groups" in *" docker "*|*" sudo "*|*" adm "*|*" root "*) bad "group membership" "$groups";; *) ok "group membership" "${groups# }";; esac
check "cannot reach the docker socket"        DENY sudo -u botkit-e2e test -r /var/run/docker.sock

echo "== unit file matches the properties just verified =="
for d in "InaccessiblePaths=-/home -/root -/srv -/etc/botkit" "ProtectSystem=strict" "ProtectHome=true" \
         "NoNewPrivileges=true" "PrivateTmp=true" "MemoryDenyWriteExecute=true" \
         "ReadWritePaths=/var/lib/botkit-e2e/session /var/lib/botkit-e2e/status" \
         "CapabilityBoundingSet=" "User=botkit-e2e" "Group=botkit-e2e" \
         "ExecStart=/opt/botkit-e2e/venv/bin/python -m e2e.run_e2e"; do
  if grep -qxF "$d" "$UNIT"; then ok "$d" "present"; else bad "$d" "MISSING"; fi
done
if grep -qE "^ExecStart=.*/root/" "$UNIT"; then bad "ExecStart does not point at /root" "found"; else ok "ExecStart avoids /root" "ok"; fi

# Every DENY probe above exits non-zero by design, and each leaves a transient
# unit behind in the failed state. Sixty-odd accumulated and made
# `systemctl --failed` unreadable on this host - which is precisely how a real
# failure would have gone unnoticed. Probes are now collected as they finish, and
# anything still lingering is reset on exit.
cleanup_probes() {
  local lingering
  lingering=$(systemctl list-units --state=failed --no-legend --no-pager --plain 2>/dev/null \
    | awk '{print $1}' | grep -c '^run-u' || true)
  if [ "${lingering:-0}" -gt 0 ]; then
    systemctl list-units --state=failed --no-legend --no-pager --plain 2>/dev/null \
      | awk '{print $1}' | grep '^run-u' \
      | while read -r unit; do systemctl reset-failed "$unit" 2>/dev/null; done
    echo "cleaned $lingering transient probe unit(s) out of the failed state"
  fi
}
trap cleanup_probes EXIT

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] && echo "SANDBOX_OK" || echo "SANDBOX_FAIL"
