## What this changes

<!-- One concern. If the diff fixes a bug and reformats a file, split it. -->

## Why

<!-- The diff already shows what changed. Say why it had to. -->

## How this was verified

<!--
This is the part that makes the change reviewable. Paste the command and its
output — "CI is green" is not evidence, because CI and your local run can both
be green about a test that cannot fail.

    ruff check --output-format=concise .
    (cd ops/e2e && PYTHONPATH=. python -m pytest -q)
    python3 scripts/check-repo-contract.py . scripts/repo-contract.json

A change that touches `ops/**/*.sh` also runs `shellcheck` and `bash -n`; one that
touches Prometheus config runs `promtool check`; one that touches compose runs
`docker compose config` and the dashboard JSON parse.
-->

- [ ] Tests pass locally, and the output is above.
- [ ] I broke the thing this guards and watched the relevant test go red.
- [ ] New tests do not need the network or a live host; anything that does
      is marked `req`.

## Documentation

- [ ] `README.md` updated if behaviour a reader would try changed.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] `AGENTS.md` updated if an agent working here would otherwise get it wrong.

## Security

- [ ] No secret, token, session string or customer identifier in the diff.
- [ ] Nothing in the alert path, the reverse-proxy routing or the secret handling
      was weakened.

## AI assistance

- [ ] Parts of this change were agent-generated — described which.
- [ ] The agent ran the verification above, in this repository, and the output is
      above. "The agent says it passes" is not evidence.
