#!/bin/sh
# certbot manual-auth-hook: publish the ACME challenge as a duckdns TXT record.
set -e
DOMAIN="ninelegsbots"
TOKEN="${DUCKDNS_TOKEN}"
CHALLENGE="${CERTBOT_VALIDATION}"

curl -s "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&txt=${CHALLENGE}" >/dev/null
# Wait for DNS propagation before certbot queries the record.
sleep 30
