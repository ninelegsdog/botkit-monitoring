# Runbook: OTLPCollectorDown / TempoDown / TempoMemoryHigh / Critical / TempoMetricsMissing

## Симптомы
- `OTLPCollectorDown` — TCP к OTLP коллектору не проходит.
- `TempoDown` — Tempo /ready не отвечает.
- `TempoMemoryHigh` / `TempoMemoryCritical` — RSS Tempo близко к cgroup-лимиту 512 MiB.
- `TempoMetricsMissing` — Tempo отвечает на /ready, но метрики процесса не поступают.

## Диагностика
1. `docker ps | grep tempo` — контейнеры Tempo и OTLP запущены?
2. `docker logs botkit-monitoring-tempo-1` — ошибки Tempo, compaction, discarded_spans.
3. `docker stats botkit-monitoring-tempo-1` — потребление памяти.
4. `curl http://127.0.0.1:3200/ready` — Tempo readiness endpoint.
5. Проверка cgroup лимитов контейнера Tempo.

## Лечение
- Если Tempo упал: `docker restart botkit-monitoring-tempo-1`.
- Если память близка к лимиту: перезапуск Tempo, увеличение cgroup-лимита.
- Если метрики не поступают: проверить scrape config Prometheus для job tempo.

## Ожидаемый результат
Tempo отвечает на /ready, метрики поступают, алерты гаснут.
