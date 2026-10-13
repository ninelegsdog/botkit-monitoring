# botkit-monitoring

Observability for the nine BotKit bots: Prometheus, Alertmanager, Grafana, Loki and
Tempo on the host network, bound to `127.0.0.1` and to nothing else.

[![ci](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/ci.yml/badge.svg)](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/ci) [![e2e-smoke](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/e2e-smoke.yml/badge.svg)](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/e2e-smoke) [![secret-audit](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/secret-audit.yml/badge.svg)](https://github.com/ninelegsdog/botkit-monitoring/actions/workflows/secret-audit) [![License](https://img.shields.io/github/license/ninelegsdog/botkit-monitoring.svg)](LICENSE)

> **Status.** Runs in production on the VPS. Every component listens on loopback only; there is no
public port for any of them. CI validates the compose file, the Prometheus config
and rules, and the YAML.

## Contents

- [What this is](#what-this-is)
- [Components](#components)
- [Getting started](#getting-started)
- [Ports and exposure](#ports-and-exposure)
- [Alerting](#alerting)
- [External heartbeat](#external-heartbeat)
- [Backups](#backups)
- [Validation](#validation)
- [Architecture decisions](#architecture-decisions)
- [Development process](#development-process)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## What this is

Observability for the nine BotKit bots: Prometheus, Alertmanager, Grafana, Loki and
Tempo on the host network, bound to `127.0.0.1` and to nothing else.

**A vendor stack, not an application** — there is no image built from this repository. Every component is an upstream container, pinned by digest.
**Loopback only** — no port on this host is exposed to the network. Grafana is reached over an SSH tunnel.
**Object storage for logs and traces** — Loki and Tempo write to MinIO over S3 rather than to local disk.

## Components

| Component | Role |
|---|---|
| Prometheus `127.0.0.1:9090` | scrapes each bot's `/metrics` on ports 8081–8089 |
| Alertmanager `127.0.0.1:9093` | routes alerts to Telegram (`telegram_configs`) |
| Grafana `127.0.0.1:3000` | the "BotKit Overview" dashboard, `instance` as a variable; reached over an SSH tunnel |
| Loki `127.0.0.1:3100` | container logs via Promtail, stored in MinIO over S3 |
| Tempo `127.0.0.1:3200` | OTLP traces via the OTel collector, stored in MinIO over S3 |
| MinIO `127.0.0.1:9000/9001` | the S3 bucket for Loki and Tempo; loopback only |

## Getting started

```bash
cd /opt/monitoring
git pull
cp .env.example .env   # TG_BOT_TOKEN, TG_CHAT_ID, GRAFANA_ADMIN_PASSWORD,
                       # MINIO_ROOT_USER, MINIO_ROOT_PASSWORD
docker compose up -d
```

## Ports and exposure

Every port below listens on `127.0.0.1` only. Nothing is exposed outward. On the
monitor `31.76.11.198`, port 3001 (Uptime Kuma) is likewise loopback-only.

| Port | Exposure |
|---|---|
| 9090 | Prometheus |
| 9093 / 9094 | Alertmanager |
| 3000 | Grafana |
| 9000 / 9001 | MinIO API / console |
| 3100 | Loki |
| 3200 | Tempo |
| 4319 / 4320 | OTel collector OTLP |

## Alerting

Four alerts, each with a `for` window, so a scrape blip is not a page:

| Alert | Fires when | For |
|---|---|---|
| BotDown | `up{job="botkit"} == 0` | 2m |
| BotErrorSpike | error rate > 0.5/s | 5m |
| BotUpdatesStalled | no updates for 30m while the process is alive | 10m |
| BotHighHandlerLatency | p95 > 5s | 10m |

## External heartbeat

An external observer, for the case where production is dead and cannot report it.
Every two minutes `ops/health/heartbeat.sh` sends a GET to a Uptime Kuma push
endpoint on the monitor `31.76.11.198` (container `uptime-kuma` 2.5.5, image pinned
by digest, bound to loopback only).

Ping path: production `127.0.0.1:3001` → SSH tunnel `botkit-kuma-tunnel.service`
(`Restart=always`, a dedicated key; on the monitor the account is `kuma-tunnel`,
`nologin` plus `restrict,port-forwarding,permitopen="127.0.0.1:3001"`) → the monitor's
loopback. No new port opens to the internet, UFW is untouched, and the token never
travels over the open network in the clear.

The Kuma monitor `botkit-prod heartbeat` (type `push`, 300 s window) goes DOWN on
silence past the window and notifies `botkit-telegram` — the same chat Alertmanager
uses. Two missed pings in the window do not raise a false alarm; a dead timer or a
dead production does, within 5–7 minutes.

Secrets live at `/root/.botkit-heartbeat/ping.url` (production) and
`/root/.uptime-kuma-admin.txt` (monitor). Web interface access from `predator`:
`ssh -N -L 13001:127.0.0.1:3001 vps`, then <http://127.0.0.1:13001>, user `botkit`.

Not healthchecks.io: the service is not reachable from the owner (403 at the edge),
so the observer is our own and lives on a machine outside production.

## Backups

Backups are documented as architecture decisions rather than a script: offsite restic over git, and RPO/RTO with backup immutability. See the ADR list below.

## Validation

```bash
promtool check config prometheus/prometheus.yml
promtool check rules prometheus/alerts.yml
docker compose config -q   # requires MINIO_* from .env
```

## Architecture decisions

- **[docs/adr/0001-offsite-backup-restic-over-git.md](docs/adr/0001-offsite-backup-restic-over-git.md)** — 0001 offsite backup restic over git.
- **[docs/adr/0002-rpo-rto-backup-immutability.md](docs/adr/0002-rpo-rto-backup-immutability.md)** — 0002 rpo rto backup immutability.

The hardening inventory this stack is held to is **[SECURITY-BASELINE.md](SECURITY-BASELINE.md)**.

## Development process

This project was built in an AI-native process: the implementation was produced by
coding agents under human-led engineering control — requirement slots, constraints
and acceptance criteria defined in advance, then specification, generation, run,
check, fix.

My role in the project:

- product definition and user-facing scenarios;
- decomposition into independent engineering stages;
- context engineering: the instructions, constraints and working rules agents run under;
- management of the context window between iterations;
- the loop of specification → generation → run → verification → correction;
- validation, testing and review;
- control of repository structure, configuration, documentation and run reproducibility.

Implementation code was generated with AI coding agents under human-led engineering
control. The full description of the process, the `AGENTS.md` template, and the
review checklists for AI-written code and secrets are in
[agentic-development-playbook](https://github.com/ninelegsdog/agentic-development-playbook).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: `yamllint` and the compose validator must pass before a pull request is opened.

## Security

Do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md), which carries the hardening baseline this stack is held to.

## License

MIT — see [LICENSE](LICENSE).
