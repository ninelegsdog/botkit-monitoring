#!/bin/sh
# certbot manual-auth-hook: publish the ACME challenge as a duckdns TXT record.
# Uses python3 (guaranteed in the certbot image; curl is not present).
set -e
export DOMAIN="ninelegsbots"
export TOKEN="${DUCKDNS_TOKEN}"
export CHALLENGE="${CERTBOT_VALIDATION}"

python3 - <<'PY'
import os, sys, urllib.request
domain = os.environ["DOMAIN"]
token = os.environ["DUCKDNS_TOKEN"]
challenge = os.environ["CHALLENGE"]
url = "https://www.duckdns.org/update?domains=%s&token=%s&txt=%s" % (domain, token, challenge)
resp = urllib.request.urlopen(url, timeout=30).read().decode().strip()
sys.stderr.write("duckdns auth response: %s\n" % resp)
sys.exit(0 if resp == "OK" else 1)
PY

# Wait for DNS propagation before certbot queries the record.
sleep 30
