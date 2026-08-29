#!/bin/sh
# certbot manual-cleanup-hook: clear the duckdns TXT record after the challenge.
set -e
DOMAIN="ninelegsbots"
TOKEN="${DUCKDNS_TOKEN}"

curl -s "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&txt=removed" >/dev/null
