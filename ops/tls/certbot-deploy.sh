#!/usr/bin/env bash
#
# certbot-deploy.sh — install a renewed certificate and PROVE the public path works.
#
# Runs on the HOST, not inside the certbot container. That distinction is the whole
# point of this rewrite:
#
#   * The certbot image has neither bash nor the docker CLI, so the previous
#     `--deploy-hook /hooks/certbot-deploy.sh` died with "env: can't execute 'bash'"
#     (rc=127) on every renewal. A renewed certificate was written into
#     /home/deploy/reverse-proxy/letsencrypt and never reached
#     /home/deploy/reverse-proxy/certs, which is what nginx reads. On 25.09 the
#     certificate reached nginx only because a human ran these steps by hand.
#   * Everything this script needs - docker, nginx -t, the bot tokens, the checker -
#     only exists on the host. So the renewal container renews, and
#     ExecStartPost= in botkit-certbot-renew.service runs this on the host.
#
# Idempotent by design: it compares the certificate public key against the one nginx
# serves and exits 0 immediately when nothing changed, so running it twice a day is
# cheap and a no-op day stays green.
#
# What it deliberately does NOT do any more: recreate all nine bot containers. The
# certificate terminates at nginx; the bots never read it. That loop restarted all of
# production on every renewal for no reason - and, worse, it made a certificate swap
# look like a fleet-wide deploy, so nobody noticed the swap itself did nothing for
# Telegram. Telegram reads the certificate when it delivers an update, so a renewal
# needs no re-registration at all; what needs checking is that the new certificate
# actually verifies for a third party and that no webhook is pinned to an old one.
#
# Usage: certbot-deploy.sh [--dry-run] [--force]
#   --dry-run  report the decision and exit, mutate nothing
#   --force    reinstall even when the public key is unchanged (exercises the full
#              path on the live certificate: backup, install, nginx -t, reload, verify)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FLEET_ENV="${BOTKIT_FLEET_ENV:-$HERE/../lib/fleet.env}"
[ -f "$FLEET_ENV" ] || FLEET_ENV=/root/botkit-webhook-check/fleet.env
if [ -f "$FLEET_ENV" ]; then
  # shellcheck disable=SC1090
  . "$FLEET_ENV"
fi
DOMAIN="${WEBHOOK_DOMAIN:-ninelegsbots.duckdns.org}"

CERT_SRC="${BOTKIT_CERT_SRC:-/home/deploy/reverse-proxy/letsencrypt/live/$DOMAIN}"
CERT_DST="${BOTKIT_CERT_DST:-/home/deploy/reverse-proxy/certs}"
NGINX_CTR="${BOTKIT_NGINX_CONTAINER:-reverse-proxy-nginx-1}"
WEBHOOK_CHECK="${BOTKIT_WEBHOOK_CHECK:-/root/botkit-webhook-check/webhook_check.sh}"
BACKUP_TAG=$(date -u +%Y%m%d%H%M%S)

DRY_RUN=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    *) echo "certbot-deploy: unknown argument $arg" >&2; exit 2 ;;
  esac
done

log() { echo "certbot-deploy[$(date -u +%H:%M:%S)]: $*"; }
fatal() { echo "certbot-deploy[$(date -u +%H:%M:%S)]: FATAL: $*" >&2; exit 1; }

pubkey_of() { # $1=cert file -> sha256 of the DER public key
  openssl x509 -in "$1" -pubkey -noout | openssl pkey -pubin -outform der | sha256sum | cut -d" " -f1
}

# ---------------------------------------------------------------- input validation
[ -s "$CERT_SRC/fullchain.pem" ] || fatal "no certificate at $CERT_SRC/fullchain.pem"
[ -s "$CERT_SRC/privkey.pem" ] || fatal "no private key at $CERT_SRC/privkey.pem"

# A certificate that expires within a day is not worth deploying; that is certbot's
# problem to solve, and shipping it would turn a renewal into an outage.
MIN_VALID_SECONDS="${BOTKIT_CERT_MIN_VALID_SECONDS:-86400}"
openssl x509 -in "$CERT_SRC/fullchain.pem" -noout -checkend "$MIN_VALID_SECONDS" >/dev/null \
  || fatal "the renewed certificate expires within ${MIN_VALID_SECONDS}s - refusing to deploy it"

cert_pub=$(pubkey_of "$CERT_SRC/fullchain.pem")
key_pub=$(openssl pkey -in "$CERT_SRC/privkey.pem" -pubout -outform der | sha256sum | cut -d" " -f1)
[ "$cert_pub" = "$key_pub" ] || fatal "certificate and private key do not match"

log "source certificate: $(openssl x509 -in "$CERT_SRC/fullchain.pem" -noout -subject -enddate | tr '\n' ' ')"
log "public key sha256: ${cert_pub:0:16}"

# ---------------------------------------------------------------- change detection
if [ -s "$CERT_DST/fullchain.pem" ]; then
  live_pub=$(pubkey_of "$CERT_DST/fullchain.pem")
else
  live_pub=""
fi

if [ "$live_pub" = "$cert_pub" ] && [ "$FORCE" = 0 ]; then
  log "UNCHANGED: nginx already serves this public key - nothing to do"
  exit 0
fi

if [ "$live_pub" = "$cert_pub" ]; then
  log "--force: public key is unchanged, reinstalling on purpose"
else
  log "CHANGED: nginx public key ${live_pub:0:16} -> ${cert_pub:0:16}"
fi

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: would back up $CERT_DST, install the new pair, nginx -t, reload, then verify"
  log "dry-run: bots from $FLEET_ENV"
  exit 0
fi

# ---------------------------------------------------------------- install with rollback
for pair in fullchain.pem privkey.pem; do
  if [ -s "$CERT_DST/$pair" ]; then
    install -m 0600 "$CERT_DST/$pair" "$CERT_DST/$pair.pre-$BACKUP_TAG"
    log "backed up $pair -> $pair.pre-$BACKUP_TAG"
  fi
done

install -m 0644 "$CERT_SRC/fullchain.pem" "$CERT_DST/fullchain.pem"
install -m 0640 "$CERT_SRC/privkey.pem" "$CERT_DST/privkey.pem"

if ! docker exec "$NGINX_CTR" nginx -t; then
  for pair in fullchain.pem privkey.pem; do
    [ -s "$CERT_DST/$pair.pre-$BACKUP_TAG" ] && install -m 0600 "$CERT_DST/$pair.pre-$BACKUP_TAG" "$CERT_DST/$pair"
  done
  docker exec "$NGINX_CTR" nginx -s reload || true
  fatal "nginx rejected the new certificate - rolled back, previous pair restored"
fi
docker exec "$NGINX_CTR" nginx -s reload
log "nginx reloaded with the new certificate"

# ---------------------------------------------------------------- prove the public path
# "The certificate was installed" is not the same claim as "updates are deliverable".
# That gap is the incident: the certificate deployed cleanly on 25.09 and Telegram
# refused every delivery for the next 15 days, because the webhooks were still pinned
# to the old self-signed public key (C2 catches exactly that).
[ -x "$WEBHOOK_CHECK" ] || fatal "webhook checker missing at $WEBHOOK_CHECK - cannot verify the public path"

if "$WEBHOOK_CHECK"; then
  log "delivery contract verified for the whole fleet after the certificate change"
else
  fatal "delivery contract FAILED after the certificate change (rc=$?). Look at
  /var/log/botkit-webhook-check.log. If a bot reports C2 (has_custom_certificate=true)
  its webhook is still pinned to an old certificate; re-register it without the
  'certificate' parameter, otherwise Telegram keeps validating against the old key:
    curl -sS -X POST \"https://api.telegram.org/bot<TOKEN>/setWebhook\" \\
      -d url=\"https://$DOMAIN/webhook/<bot>\" \\
      -d secret_token=\"<TELEGRAM_WEBHOOK_SECRET>\" -d ip_address=\"<WEBHOOK_IP>\""
fi
