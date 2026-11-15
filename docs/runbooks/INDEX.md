# Runbooks для botkit-monitoring

Этот каталог содержит процедуры реагирования на алерты из `prometheus/alerts.yml`.
Каждый файл соответствует одной группе алертов или отдельному алерту.

## Соответствие алертов и runbook

| Алерт | Runbook | Статус |
|-------|---------|--------|
| BotkitHealthcheckStale / BotkitHealthcheckFailed | `healthcheck.md` | ✅ |
| BotkitRolloutCheckStale / BotkitRolloutCheckFailed | `rollout-check.md` | ✅ |
| BotkitImagePruneStale / BotkitImagePruneFailed | `image-prune.md` | ✅ |
| BotkitDriftStale / BotkitDriftDetected | `drift.md` | ✅ |
| BotkitBackupStale / BotkitBackupFailed / BotkitBackupCheckMissing / BotkitBackupRunFailed / BotkitBackupRunMetricMissing | *(в общем бэкап-рунбуке)* | ⚠️ |
| BotkitBackupVerifyStale / BotkitBackupVerifyFailed | `backup-verify.md` | ✅ |
| BotErrorSpike / BotErrorBudgetBurnFast / Medium / Slow / BotMetricsUnavailable / BotHighHandlerLatency / BotHandlerLatencySLOBreach | `errors.md` | ✅ |
| BotWebhookDown / BotWebhookLatencySLOBreach / BotWebhookTLSUntrusted / BotWebhookCheckStale | `webhook.md` | ✅ |
| BotDown / BotCrmOutboxPendingHigh / Critical / BotHighHandlerLatency / BotHandlerLatencySLOBreach | `bots.md` | ✅ |
| HostDiskSpaceHigh / HostMemHigh / HostMemoryPressureHigh / Critical / HostLoadHigh / HostUnreachable | `host.md` | ✅ |
| OTLPCollectorDown / TempoDown / TempoMemoryHigh / Critical / TempoMetricsMissing | `tempo.md` | ✅ |
| SSLCertExpiring / SSLCertExpired | `ssl.md` | ✅ |
| PrometheusDown / AlertmanagerDown / AlertmanagerNotificationFailures | `monitoring-self.md` | ✅ |
| MonitorVPSDown | `monitoring-vps.md` | ✅ |

Статус: ✅ — файл существует, ⚠️ — частично (общий бэкап-рунбук планируется отдельно).

## Как использовать

При срабатывании алерта откройте указанный runbook и следуйте шагам диагностики в разделе «Проверить».
Если runbook не существует, создайте Issue в репозитории с пометкой «runbook missing».
