# Эксплуатация VPN Dashboard

## Backup и восстановление

`deploy/scripts/backup-dashboard.sh` создаёт консистентный SQLite snapshot, сохраняет закрытую конфигурацию dashboard и persistent-конфигурацию VPN-шлюза в зашифрованный Restic-репозиторий. Пароль Restic хранится отдельным файлом с правами `0600`; он не попадает в Git или Hyper Backup логи.

Synology Task Scheduler запускает backup ежедневно в 03:30 `Asia/Yekaterinburg`. Hyper Backup должен реплицировать каталог `/volume1/docker/vpn-gateway/backups/dashboard/restic`. Раз в месяц запускается `restore-verify.sh`; он выполняет `restic check`, восстановление в scratch-каталог и `PRAGMA integrity_check`.

Перед аварийным восстановлением остановите dashboard, восстановите последнюю snapshot-копию, верните `dashboard.env` с исходным `DASHBOARD_ENCRYPTION_KEY`, затем запускайте проект. Новый ключ шифрования без миграции сделает сохранённые credentials wg-easy недоступными.

## Безопасное развёртывание

Перед первым запуском hardened-версии root выполняет `deploy/scripts/prepare-runtime.sh`. Он проверяет обязательный ключ, требует localhost-bind, переводит только данные dashboard и его управляемые rule-файлы к непривилегированному UID `10001`, затем запускает `docker compose config --quiet`. Скрипт не перезапускает `vpn-wireguard`, Mihomo или wg-easy.

DSM Reverse Proxy должен направлять `https://roaring.crazedns.ru:10443` на `http://127.0.0.1:8088`. Прямой доступ к `8088` из LAN и WAN после этого отсутствует.
