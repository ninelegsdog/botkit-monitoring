# Runbook: BotkitImagePruneStale / BotkitImagePruneFailed

## Симптомы
- `BotkitImagePruneStale` — метрика `botkit_image_prune_last_run_seconds` старше 8 суток или отсутствует.
- `BotkitImagePruneFailed` — метрика `botkit_image_prune_ok == 0` (не удалось удалить некоторые теги).

## Диагностика
1. `systemctl status botkit-image-prune.timer` — таймер активен (еженедельный).
2. `journalctl -u botkit-image-prune` — ошибки последнего прогона.
3. `ls -l /var/lib/node-exporter-textfile/botkit_image_prune.prom` — файл существует, права 644.
4. `docker system df` — сколько места занимают образы.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-image-prune.timer`.
- Если диск заполняется образами: `docker system prune -a --filter "until=720h"` вручную, затем проверить таймер.
- Если юнит падает до записи метрики: проверить ReadWritePaths и права на каталог textfile.

## Ожидаемый результат
Метрика `botkit_image_prune_last_run_seconds` свежая, `botkit_image_prune_ok == 1`, алерт гаснет.
