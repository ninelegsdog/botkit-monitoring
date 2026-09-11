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
