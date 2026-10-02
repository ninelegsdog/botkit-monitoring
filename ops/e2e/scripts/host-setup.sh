#!/usr/bin/env bash
# Idempotent bootstrap for the E2E host. Run as root; it reads the sudo password
# from stdin once and never stores it.
#
#   printf '%s\n' "$PW" | sudo -S -p '' bash ops/e2e/scripts/host-setup.sh
#
# What it creates, and why each piece:
#   botkit-e2e       system account, nologin, no home. Not in sudo and not in the
#                    docker group - that group is root in all but name, which would
#                    defeat the whole sandbox.
#   /opt/botkit-e2e  a single git clone, owned root:botkit-e2e 0750 with o-rwx.
#                    A copy cannot keep itself current; a clone can, and the 26.09
#                    incident was exactly a unit pointing at a stale copy.
#   /var/lib/...     session and status outside $HOME, because ~/bin/backup.sh on
#                    this host encrypts $HOME/.ssh and ships it elsewhere.
#   e2e.env          root:botkit-e2e 0640, and no TG_PHONE by design: strategy C
#                    signs the session in once on an admin machine.
#
# The runner's own isolation is asserted by scripts/sandbox-check.sh, not here.
set -euo pipefail

SUDO_PASS="$(cat)"
sudo -S -p "" -v <<<"$SUDO_PASS"
run() { sudo -S -p "" "$@"; }

RUNNER_USER=${RUNNER_USER:-botkit-e2e}
CLONE=${CLONE:-/opt/botkit-e2e}
REPO_URL=${REPO_URL:-https://github.com/ninelegsdog/botkit-monitoring}

echo "== 1. runner account =="
if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
  run useradd --system --no-create-home --shell /usr/sbin/nologin "$RUNNER_USER"
  echo "  created $RUNNER_USER"
else
  echo "  $RUNNER_USER exists"
fi
groups=$(id -Gn "$RUNNER_USER")
for forbidden in docker sudo adm root; do
  case " $groups " in
    *" $forbidden "*) echo "  FATAL: $RUNNER_USER is in group $forbidden; remove it first" >&2; exit 1;;
  esac
done
echo "  groups: $groups"

echo
echo "== 2. canonical clone =="
if [ ! -d "$CLONE/.git" ]; then
  run git clone -q "$REPO_URL" "$CLONE"
  echo "  cloned $REPO_URL"
else
  echo "  already present at $CLONE"
fi
run chown -R "root:$RUNNER_USER" "$CLONE"
run chmod 0750 "$CLONE"
run chmod -R o-rwx "$CLONE"

echo
echo "== 3. state directories, outside \$HOME =="
run install -d -o root -g "$RUNNER_USER" -m 0750 /etc/botkit-e2e
run install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0700 /var/lib/botkit-e2e/session
run install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0750 /var/lib/botkit-e2e/status

echo
echo "== 4. venv from the pinned requirements =="
if [ ! -x "$CLONE/venv/bin/python" ]; then
  run python3 -m venv "$CLONE/venv"
  run chown -R "$RUNNER_USER:$RUNNER_USER" "$CLONE/venv"
  run chmod -R o-rwx "$CLONE/venv"
  run "$CLONE/venv/bin/pip" install -q --upgrade pip
  run "$CLONE/venv/bin/pip" install -q -r "$CLONE/ops/e2e/requirements.txt"
  echo "  built"
else
  echo "  exists"
fi

echo
echo "== 5. env file: keys only, never values =="
ENV=/etc/botkit-e2e/e2e.env
if [ ! -f "$ENV" ]; then
  cat > "$ENV" <<EOF
# E2E runner environment. root:botkit-e2e 0640.
# TG_PHONE is deliberately NOT here: under strategy C the session is signed in
# once on an admin machine and the phone number never reaches the host.
# E2E_ALERT_URL is intentionally empty (owner decision B5=c): the E2E host cannot
# reach the production Alertmanager, and the runner prints FATAL and writes
# <bot>.fail on every failure, so nothing fails silently - it just does not reach
# Telegram until the S phase moves Alertmanager to the monitor.
#
# Fill in the two values below, then: chmod 0640 $ENV
TG_API_ID=
TG_API_HASH=

E2E_TIMEOUT=30
E2E_BOTS_FILE=$CLONE/ops/e2e/bots.yml
E2E_SESSION_DIR=/var/lib/botkit-e2e/session
E2E_SESSION_NAME=botkit-e2e
E2E_STATUS_DIR=/var/lib/botkit-e2e/status
E2E_SCENARIOS=$CLONE/ops/e2e/scenarios.yml
E2E_ALERT_URL=
E2E_PROXY=socks5:127.0.0.1:11080
EOF
  run chown "root:$RUNNER_USER" "$ENV"
  run chmod 0640 "$ENV"
  echo "  created with TG_API_ID and TG_API_HASH empty - fill them in yourself"
else
  echo "  exists, left untouched"
fi

echo
echo "== 6. units: installed but not armed =="
run install -m 0644 "$CLONE/ops/e2e/systemd/botkit-e2e.service" /etc/systemd/system/botkit-e2e.service
run install -m 0644 "$CLONE/ops/e2e/systemd/botkit-e2e.timer" /etc/systemd/system/botkit-e2e.timer
run systemctl daemon-reload
# The timer stays disabled until W20, after the live run and the negative check.
echo "  botkit-e2e.timer: $(systemctl is-enabled botkit-e2e.timer 2>/dev/null || echo disabled) (arming it is W20)"
run systemctl show botkit-e2e.service -p ExecStart --value

echo
echo "== 7. the tunnel is a separate step =="
echo "  botkit-e2e-tunnel needs its own user, key and an entry on the monitor."
echo "  See the unit file and the ADR addendum; it is deliberately not created"
echo "  here, because it writes to a file on another machine."
echo
echo "HOST_SETUP_OK - next: sandbox-check.sh, then the first login from an admin machine"