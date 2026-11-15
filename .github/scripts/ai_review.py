import json
import os
import urllib.request

with open("/tmp/pr.diff") as f:
    diff = f.read()

if not diff.strip():
    with open("/tmp/review.txt", "w") as f:
        f.write("No diff; skipping.")
    raise SystemExit(0)

system_msg = (
    "You are a senior code reviewer. Review the PR diff. Be concise. "
    "Point out bugs, security issues, and missing tests. If the change "
    "looks fine, say so. Do not repeat the diff. Answer in Russian."
)

body = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": "Review this diff:\n\n" + diff},
    ],
    "max_tokens": 800,
    "temperature": 0.2,
}

token = os.environ["GITHUB_TOKEN"]
req = urllib.request.Request(
    "https://models.inference.ai.azure.com/chat/completions",
    data=json.dumps(body).encode(),
    headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    msg = d["choices"][0]["message"]["content"]
except (OSError, ValueError, KeyError) as e:
    msg = "AI review unavailable: " + str(e)

with open("/tmp/review.txt", "w") as f:
    f.write(msg)
