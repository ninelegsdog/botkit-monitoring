# Security Policy
## Hardening baseline

The full hardening inventory lives in **[SECURITY-BASELINE.md](SECURITY-BASELINE.md)**
and is not duplicated here. It is the source of truth for what this stack is
held to; this file states the commitments, the baseline states the detail.


## Reporting a vulnerability

**Do not open a public issue.**

Email the maintainer directly, or use GitHub's private vulnerability reporting at
<https://github.com/ninelegsdog/botkit-monitoring/security/advisories/new>. Include:

- what the issue is and what an attacker gains;
- steps to reproduce;
- the affected version or commit.

You should get an acknowledgement within a few days. Fixes for confirmed issues
ship in a patch release, and the reporter is credited unless they prefer otherwise.

## Scope

In scope:

- the webhook endpoint and the authentication that guards it;
- the admin panel and its authorisation;
- secrets handling and the CI workflow's permissions;
- dependency compromise affecting this project.

Out of scope:

- vulnerabilities in aiogram, SQLAlchemy or Redis — report those upstream;
- findings that require an attacker who already has write access to the repository;
- the production host's network perimeter, which is not controlled here.

## Design commitments

These are properties this project holds itself to. A change that breaks one is a
security change and needs review.

1. **Webhook traffic is authenticated.** Every inbound event is checked against the
   configured secret header before it is parsed. An unauthenticated event never
   reaches the state machine.
2. **Admin actions are gated by id and password.** The admin panel is not reachable
   by being a member of a chat.
3. **Secrets never enter git.** `.env` is ignored, `.env.example` carries no real
   values, and CI runs gitleaks over the full history rather than only the tip.
4. **The container does not run as root.** It drops all capabilities and runs
   `read_only` with a tmpfs for the paths that genuinely need to be written.
5. **The dependency set is pinned.** `uv.lock` is enforced current by CI, so a
   resolved dependency set cannot drift while the build stays green.

## Supported versions

This project is versioned together with the nine production Telegram bots on Python family. Security fixes land on
`main` and are cut from there. No long-term-support branch exists yet.
