# Отчёт о выполнении плана доработок (после расследования причин prevention)

Дата: 2026-11-14 (после перезапуска сервера; продолжение).
Основание: `/tmp/opencode/root_cause_post_prevention.md`, раздел «Почему дефекты prevention
появились», SKILL.md.

## Выполнено в этой сессии

### D1 — Runbooks ✅
- Создан `docs/runbooks/` (INDEX.md, healthcheck.md, rollout-check.md, image-prune.md, drift.md,
  backup-verify.md, errors.md, webhook.md, bots.md, host.md, tempo.md, ssl.md, monitoring-self.md,
  monitoring-vps.md, backup.md, smoke.md).
- Обновлено 39 мёртвых `runbook_url` (было `tree/main/docs/runbooks` → конкретные файлы).
- `tests/test_runbook_urls_exist.py` создан и проходит (2 passed).

### D2 — botkit_errors_total инициализация ✅
- `init_error_metrics()` добавлена в bot error handler (`BOTKIT-FARM/bots/botkit-store/src/core/errors.py`).
- Pre-initialize error_type series (retry_after, network, unhandled, domain) с 0.

### D3 — BotMetricsUnavailable исправлен ✅
- Выражение переписано: `and on(instance) absent_over_time(...)` → `unless on(instance) count by (instance) (...)`.
- YAML валиден, `promtool check rules` пройден ранее — пересмотр нужен после reload.

### D4 — Smoke self-staleness ✅
- `ops/e2e/smoke_all.sh` добавлена запись `botkit_smoke_last_run_seconds` в textfile.
- `prometheus/alerts.yml` добавлен `BotkitSmokeStale`.
- Тест `test_smoke_has_staleness_alert` в `tests/test_alert_hygiene.py` — passed.

### D5 — Dependabot auto-merge ✅
- `allow_auto_merge=true` включён в репозитории (gh api PATCH).
- `rebase-strategy: auto` добавлен в `.github/dependabot.yml`.

### D6 — host-check dynamic ✅
- `ops/lib/host-check.sh` — список poller metrics выведен из alerts.yml (python3 + yaml).

### D7 — test_alert_hygiene coverage ✅
- Добавлен `test_stale_and_failed_pairs_exist_for_every_poller` с known-exceptions.
- Все 6 тестов `tests/test_alert_hygiene.py` — passed.

### D8 — test_runbook_urls_exist ✅
- Создан и проходит (2 passed).

### D9 — test_restic_verify_metrics ✅
- Создан `tests/test_restic_verify_metrics.py` — passed (метрика в файл, а не stdout).

### D10 — issue #95 обновлён ✅
- Body переписан (webhook уже покрыт с 27.09, smoke добавлен).

### D12 — sync-canon metric ✅
- `ops/lib/sync-canon.sh` — добавлена метрика `botkit_sync_canon_last_run_seconds`.

### Валидация ✅
- YAML alerts.yml — OK (47 rules).
- shellcheck — OK.
- bash -n — OK.
- pytest core (не fleet) — 467 passed, 14 skipped.

### Замечания / неполноты
- D11 (`botkit_clone_age_seconds`) — не выполнен (P2, сложный).
- D13 (textfile_write refactor для остальных writers) — не выполнен (P2).
- D14–D18 — owner-gated, блокировано у владельца.
- BotErrors init в deployed-venv не перезапущен — нужен redeploy бота для применения.

## Следующий шаг
- PR + squash-merge в main, sync-canon на prod, redeploy ботов (для применения D2).

## Знания
- Отчёт: `/tmp/opencode/root_cause_post_prevention.md`
- SKILL.md: раздел «Почему дефекты prevention появились».
- Vault: `2026-11-14-prevention-plan.md` (обновлено).
- Все тесты: `tests/test_alert_hygiene.py`, `tests/test_runbook_urls_exist.py`, `tests/test_restic_verify_metrics.py` — passed.
