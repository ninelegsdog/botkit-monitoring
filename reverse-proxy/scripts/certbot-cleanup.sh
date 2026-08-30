#!/bin/sh
# certbot manual-cleanup-hook: clear the duckdns TXT record after the challenge.
# Uses python3 (guaranteed in the certbot image; curl is not present).
set -e
export DOMAIN="ninelegsbots"
export TOKEN="${DUCKDNS_TOKEN}"

python3 - <<'PY'
import os, sys, urllib.request
domain = os.environ["DOMAIN"]
token = os.environ["TOKEN"]
url = "https://www.duckdns.org/update?domains=%s&token=%s&txt=removed" % (domain, token)
resp = urllib.request.urlopen(url, timeout=30).read().decode().strip()
sys.stderr.write("duckdns cleanup response: %s\n" % resp)
PY
