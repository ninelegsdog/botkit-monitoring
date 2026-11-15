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

### D2 — botkit_errors_total инициализация ✅ (выкачено на прод 15.11)
- `init_error_metrics()` добавлена в `src/core/errors.py` восьми bot-репозиториев:
  `botkit-store` (#61), `bookingbot` (#59), `membership` (#57), `leadgen` (#58),
  `support` (#59), `pricesentry` (#56), `docuflow` (#55), `delivery` (#49) — все squash-merged.
- Pre-initialize error_type series (retry_after, network, unhandled, domain) с 0 при
  регистрации хендлера.
- deploy.yml по всем 8 — success; rollout-check на проде перевёл контейнеры на новые
  теги (`v0.8.5-<sha>`), health/metrics OK.
- На проде: `count by (instance) (botkit_errors_total)` = 4 у каждого из 8 ботов
  (`bot-8081..8088`), всего 32 серии. SLO-правила больше не инертны.
- `botkit-reminder` не покрыт: он публикует `error_handler_errors_total`
  (`error_type=retry_after|network_error|unhandled`), а не `botkit_errors_total` —
  отдельный gap, issue #111.

### D3 — BotMetricsUnavailable исправлен ✅
- Выражение переписано: `and on(instance) absent_over_time(...)` → `unless on(instance) count by (instance) (...)`.
- При merge PR #110 эта правка откатилась (см. «Замечания»), восстановлена PR #112.
- На проде после reload: expr = `... > 0.001 unless on (instance) count by (instance) (botkit_errors_total{job="botkit"})`.

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

### Прод-статус 15.11 ✅
- `sync-canon` → `SYNC OK 684da6c`; Prometheus reload.
- 47 rules, `BotkitSmokeStale` присутствует, expr `BotMetricsUnavailable` — `unless on (instance) count by (...)`.
- Active alerts: 0.
- `botkit_errors_total`: 32 серии (8 ботов × 4 error_type).
- `botkit_smoke_last_run_seconds`: 1 серия; `count(botkit_clone_age_seconds)` = 9;
  `botkit_drift_last_run_seconds`, `botkit_backup_last_run_seconds` — свежие.

### Валидация ✅
- YAML alerts.yml — OK (47 rules).
- promtool check rules — SUCCESS: 47 rules found.
- shellcheck (все `ops/**/*.sh`) — OK.
- bash -n (все `ops/**/*.sh`) — OK.
- pytest core (не fleet) — 469 passed, 14 skipped (+2 теста #114).
- main после docs-PR #115: `684da6c` (этот файл).
- Профильные контракты (textfile_mode, image_prune, drift, smoke, backup, restic, health/rollout) — 62 passed.

### Замечания / неполноты
- D11 (`botkit_clone_age_seconds`) — выполнен в `ops/drift/check_drift.sh` (коммит `1434f7d`).
- D13 (textfile_write refactor) — выполнен: все писатели textfile используют общий
  helper `write_textfile` (`check_updates.sh`, `prune_images.sh`, `restic-check.sh`,
  `restic-backup.sh`, `check_drift.sh`, `smoke_all.sh`; `healthcheck.sh` — ранее).
  Тестовые harness'ы копируют `ops/lib/textfile_write.sh` рядом с `fleet.sh`.
- D14–D18 — owner-gated, блокировано у владельца.
- **Ловушка merge (15.11):** резолюция конфликта `alerts.yml` в PR #110 взяла main как
  базу и вернула только runbook-URL + `BotkitSmokeStale`; правка D3 (`BotMetricsUnavailable`)
  откатилась к `and on(instance) absent_over_time`, т.е. к молчащему правилу. Поймано
  структурным diff'ом alert→expr, восстановлено PR #112. Вывод: при merge-резолюции
  сравнивать не имена алертов, а `expr`/`annotations` по каждому правилу.

- Финал 15.11: PR #114 добавил 2 контрактных теста на форму `BotMetricsUnavailable`
  (запрет `and on(instance)`, требование `absent_over_time`). main = `9f3d993`.
  Знания разложены: vault `30_LEARNINGS/2026-11-14-prevention-root-cause.md`,
  `40_SESSIONS/2026-11-15-prevention-status.md`, `40_SESSIONS/2026-11-14-prevention-checklist.md`,
  `40_SESSIONS/2026-11-14-prevention-plan.md`; SKILL.md (раздел «Реабилитация prevention»).
- Farm-smoke (`Ферма · офлайн-прогоны`) красный и на `main` с 14.11 — pre-existing,
  не required-check (required только `check`, `security`), поэтому не блокирует merge.
- `botkit-reminder` — gap `botkit_errors_total`, issue #111.

## Следующий шаг
- Owner-gated: #99 AI-ревью PR, #103 restore-drill/RTO, #104 ротация секретов,
  #105 E2E-аккаунт, #106 kb-gate, #111 reminder-метрика.

## Знания
- Отчёт: `/tmp/opencode/root_cause_post_prevention.md`
- SKILL.md: раздел «Почему дефекты prevention появились».
- Vault: `2026-11-14-prevention-plan.md` (обновлено).
- Все тесты: `tests/test_alert_hygiene.py`, `tests/test_runbook_urls_exist.py`, `tests/test_restic_verify_metrics.py` — passed.
