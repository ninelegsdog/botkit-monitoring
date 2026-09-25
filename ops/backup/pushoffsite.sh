#!/bin/bash
# Push exported backups to private GH repo (offsite copy).
set -u
WORK=/tmp/offsite
GH="${GH_REPO:-ninelegsdog/botkit-backups-offsite}"
rm -rf "$WORK"
TOKEN=$(cat /etc/git-token 2>/dev/null || true)
[ -z "${TOKEN:-}" ] && { echo "$(date -Is) [offsite] ERROR: no token" >> /export/export.log; exit 2; }
ASKPASS=/git-askpass.sh
trap 'rm -f "$ASKPASS"' EXIT
cat > "$ASKPASS" <<'EOF'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) tr -d '\r\n' < /etc/git-token ;;
esac
EOF
chmod 700 "$ASKPASS"
if ! GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 "https://github.com/${GH}.git" "$WORK" 2>/dev/null; then
  echo "$(date -Is) [offsite] ERROR: clone failed" >> /export/export.log; exit 3
fi
mkdir -p "$WORK/snapshots"
cp -r /export/* "$WORK/snapshots/"
git -C "$WORK" add -A >/dev/null 2>&1 || true
git -C "$WORK" -c user.name="offsite-backup" -c user.email="offsite@local" commit -q -m "snapshot $(date -Is)" 2>&1 | sed -E 's#https://[^@]*@#https://#' >> /export/export.log
if GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git -C "$WORK" push --quiet origin HEAD >> /export/export.log 2>&1; then
  echo "$(date -Is) [offsite] ok: $(ls /export | tr '\n' ' ')" >> /export/export.log
else
  echo "$(date -Is) [offsite] ERROR: push failed" >> /export/export.log
  exit 4
fi
rm -rf "$WORK"
