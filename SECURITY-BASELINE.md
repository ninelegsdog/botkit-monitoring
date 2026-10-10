# SECURITY-BASELINE prod 2.27.204.95 (bump 2026-09-11)

## Аудит 11.09 — статусы
| Контроль | Статус |
|---|---|
| SSH: PasswordAuthentication no + PermitRootLogin prohibit-password | ✅ |
| UFW: только 22/tcp 80 443 37426/udp | ✅ |
| fail2ban sshd+recidive (maxretry 3, bantime 1h→7d recidive) | ✅ |
| fail2ban ignoreip: 31.76.11.198 5.144.123.123 5.144.122.222 | ✅ ДОБАВЛЕН (корень блокировки predator) |
| Docker: user 1001:1001, cap_drop ALL, no-new-privileges, tmpfs /tmp | ✅ 9/9 ботов (compose.yml) |
| Боты bind 127.0.0.1 (не экспонированы) | ✅ ss -tlnp |
| nginx TLS 1.2/1.3, fullchain certs | ✅ |
| .env → chmod 600 | ✅ 11 файлов |
| Unattended-upgrades | ✅ active |
| grub-pc dpkg half-configured | ✅ ПОЧИНЕНО 11.09 (install_devices sr0→vda) |
| apt update/dpkg audit | ✅ clean, 0 errors |
| .bak мусор на проде | ✅ вычищен (0 осталось) |
| Docker Root Dir | ⚠️ /var/lib/docker (72% диск) — мониторить |

## Бэкапы за пределами хоста (добавлено 2026-09-27)

- Offsite-копия шифруется `restic` (AES-256), репозитории живут на мониторе
  `31.76.11.198`, недоступны из интернета и изолированы `chroot` для пользователя
  `botkit-backup`.
- Два репозитория с двумя независимыми паролями: знание одного не даёт доступа к другому.
- Пароли хранятся только на проде в `/root/.botkit-backup/*.pw` (600) и **не имеют
  резервной копии в репозитории** — это осознанный компромисс, компрометация прода
  не даёт доступа к бэкапам без пароля, но потеря пароля означает потерю бэкапов.
  Требование: продублировать в менеджер паролей владельца.
- Свежесть offsite проверяется машинно (`botkit-restic-check.timer` → textfile-метрика
  → алерт `BotkitBackupStale`), а не «на глаз»: именно отсутствие такой проверки
  скрыло падение offsite на 15 дней.
- Git-доступ к бэкапам запрещён: общая судьба с кодом, нет шифрования, нет удаления,
  нет retention.
## Периметр 10.10.2026 (1.3, dev-режим) — эталон доступа по SSH

> Обновлено после 1.3: сужение 22/tcp, удаление `50-cloud-init.conf`, fail2ban на мониторе,
> эталон sshd. **Главный фильтр по 22 на проде — `/etc/nftables.conf` (priority -50, работает
> РАНЬШЕ UFW).** UFW — второй рубеж. Оба списка синхронизированы.

### 1. `/etc/nftables.conf` на проде (главный периметр, `table inet fw`, priority -50)

Ключевые правила (после правки 10.10):

```
ip saddr { 10.8.1.0/24, 31.76.11.198, 81.201.16.75, 109.236.105.106, 109.236.105.237 } tcp dport 22 accept
tcp dport 22 drop
tcp dport { 80, 443 } accept
tcp dport { 6380, 3100, 3200, 4317, 4318, 4319, 4320, 9080, 9095, 9096, 9097 } drop   # явные правила мониторинга
udp dport 37426 accept                                                                 # amnezia VPN
ip saddr { 172.30.0.0/16, 172.31.0.0/16, 192.168.*.0/20 } tcp dport { 4317, 4318 } accept  # OTLP от bot-мостов
```

Источники 22: `10.8.1.0/24` (VPN awg0), `31.76.11.198` (монитор), `109.236.105.237` (predator),
`81.201.16.75` + `109.236.105.106` (прежние/резервные входы), `5.144.123.123`/`5.144.122.222`
(владелец — только в UFW, в nftables не добавлены сознательно: UFW их пропускает, nftables 22
лимитирован перечисленным, для владельца путь = VPN или монитор).

- **Урок 10.10:** `tcp dport 22 drop` в nftables (без нового источника) даёт `connect timeout`,
  НЕ отказ — из-за него e2e-smoke (GH Actions) и `predator→prod:22` выглядели как «провайдер
  режет сеть». Диагноз после добавления `109.236.105.237` в лист: `predator→prod:22` REACHABLE.
- Бэкап до правки: `/root/backup-nftables.conf.1.3b`.

### 2. UFW на проде — 22/tcp только точечно (после 1.3)

```
22/tcp ALLOW IN 31.76.11.198        # ssh: monitor (jump)
22/tcp ALLOW IN 109.236.105.237     # ssh: predator
22/tcp ALLOW IN 10.8.1.0/24         # ssh: vpn awg0
22/tcp ALLOW IN 5.144.123.123       # ssh: owner1
22/tcp ALLOW IN 5.144.122.222       # ssh: owner2
22/tcp ALLOW IN 81.201.16.75        # ssh: legacy entry (nftables list)
22/tcp ALLOW IN 109.236.105.106     # ssh: legacy entry (nftables list)
```

`22/tcp Anywhere` (v4+v6) удалён. Остальные порты как прежде (80, 443, 37426/udp).

### 3. sshd-эталон

**Прод** (`/etc/ssh/sshd_config.d/`): `10-hard.conf`, `51-e2e-am-tunnel.conf`, `99-hard.conf`.
Проверено 10.10: `passwordauthentication no`, `allowtcpforwarding no` (глобально),
`maxauthtries 4`; исключение — Match `e2e-am-tunnel` (permitopen 127.0.0.1:9093).

**Монитор** (10.10): `50-cloud-init.conf` удалён; `10-hardening.conf` переписан
(Password/Kbd/Challenge no, PermitRootLogin prohibit-password, X11Forwarding no,
AllowTcpForwarding no, MaxAuthTries 4, MaxSessions 4, LoginGraceTime 60,
ClientAlive 300/3, LogLevel VERBOSE); `51-jumphost.conf` — `Match User deploy` →
`AllowTcpForwarding yes` + `PermitOpen 2.27.204.95:22` (единственный инструмент доступа к проду).
Бэкапы: `/root/backup-ssh-1.3/` на мониторе.

### 4. fail2ban

- **Прод:** `jail.d/custom.conf` (sshd+recidive), `jail.local` ignoreip `127.0.0.1/8 ::1 31.76.11.198`.
- **Монитор (установлен 10.10):** apt-пакет, `jail.d/custom.conf` (sshd maxRetry 3 banTime 3600;
  recidive banTime 604800) + `jail.local` ignoreip **обязателен** с predator `109.236.105.237` и
  прод `2.27.204.95` (иначе теряем jumphost-путь и offsite-бэкапы restic, которые ходят
  прод→монитор).
- fail2ban на проде использует action nftables; фактических банов нет (0/0), история чистая.

### 5. promtail / otelcol (явные правила, не loopback)

- **otelcol оставлен на `0.0.0.0:4317/4318`** — принимает OTLP от ботов через docker-мост
  (`172.17.0.1:4318`); извне закрыт UFW+nftables (приватные подсети). Loopback сломал бы
  телеметрию. Требование «127.0.0.1 ИЛИ явные правила» выполнено явными правилами nftables.
- **promtail** слушает `*:9080/9097`, пушит в локальный Loki `127.0.0.1:3100`; снаружи 9080/9097
  закрыт nftables-листом drop (см. п.1). Локальный скрейп/health не затронут.### 6. e2e-smoke (GH Actions → прод) — починен 10.10 через монитор-джамп

- **Корень (c 10.09):** `/etc/nftables.conf` на проде дропает `22` для всех вне allow-list;
  общих ranges GitHub-раннеров там нет и _не будет_ (динамические Azure-подсети).
  Поэтому прямой ssh GH→прод:22 невозможен.
- **Решение 10.10 (выбор владельца):** e2e-smoke идёт через монитор 31.76.11.198
  (тяжёлый jumphost). В workflow `e2e-smoke.yml`:
  - внешний ssh — прежний `SSH_KEY` на прод, но `ProxyCommand` через монитор;
  - новый выделенный ключ CI `JUMP_KEY` (секрет GH) с **жёстким ограничением** в
    `/home/deploy/.ssh/authorized_keys` на мониторе:
    `permitopen="2.27.204.95:22",command="/bin/false",no-agent-forwarding,no-X11-forwarding,no-pty,no-user-rc`
    — форвард только на прод:22, шелл на мониторе запрещён (verified 10.10);
  - `JUMP_HOST` = `31.76.11.198` (секрет GH).
- **Джуна:** при переносе ключа в GH-секрет `gh secret set` срезает завершающий `\n`,
  OpenSSH не парсит такой ключ (`error in libcrypto`) → в workflow обязателен
  `printf '%s\n'` при записи ключей в файлы.
- **Пользователь:** smoke_all.sh пишет в `/var/log` (root), GH-прогон идёт под `deploy` →
  workflow экспортирует `BOTKIT_SMOKE_LOG`/`BOTKIT_SMOKE_ALERTED_DIR` в `/home/deploy/botkit-smoke/`.
- **Итог:** `gh workflow run e2e-smoke` → `SMOKE_ALL: PASS` (9/9 ботов, 10.10 02:41).
  Локальный `botkit-smoke.timer` (root, /var/log) работает независимо, каждый 30 мин.