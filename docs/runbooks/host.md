# Runbook: HostDiskSpaceHigh / HostMemHigh / HostMemoryPressureHigh / Critical / HostLoadHigh / HostUnreachable

## Симптомы
- `HostDiskSpaceHigh` — диск занят более 85%.
- `HostMemHigh` — RAM занята более 92%.
- `HostMemoryPressureHigh` — более 1% времени задачи стоят на памяти (PSI).
- `HostMemoryPressureCritical` — более 5% времени задачи стоят на памяти.
- `HostLoadHigh` — load average 1m > 6 в течение 15 минут.
- `HostUnreachable` — node_exporter недоступен 3+ минут.

## Диагностика
1. `df -h` — какие разделы заполнены.
2. `docker system df` — сколько места занимают образы, контейнеры, тома.
3. `free -m; cat /proc/pressure/memory` — давление памяти.
4. `uptime; top` — какие процессы потребляют ресурсы.
5. `docker stats --no-stream --sort=-mem` — крупнейшие потребители памяти.
6. `systemctl status botkit-monitoring-node-exporter` — node_exporter живой?

## Лечение
- Для диска: `docker system prune -a`, удаление старых логов, ротация.
- Для памяти: остановка крупнейшего потребителя, увеличение лимита хоста.
- Для load: поиск процесса, потребляющего CPU, оптимизация или масштабирование.

## Ожидаемый результат
Ресурсы хоста в норме, алерты гаснут.
