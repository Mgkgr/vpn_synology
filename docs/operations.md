# Эксплуатация VPN Dashboard

## Backup и восстановление

`deploy/scripts/backup-dashboard.sh` создаёт консистентный SQLite snapshot, сохраняет закрытую конфигурацию dashboard и persistent-конфигурацию VPN-шлюза в зашифрованный Restic-репозиторий. Пароль Restic хранится отдельным файлом с правами `0600`; он не попадает в Git или Hyper Backup логи.

Synology Task Scheduler запускает backup ежедневно в 03:30 `Asia/Yekaterinburg`. Hyper Backup должен реплицировать каталог `/volume1/docker/vpn-gateway/backups/dashboard/restic`. Раз в месяц запускается `restore-verify.sh`; он выполняет `restic check`, восстановление в scratch-каталог и `PRAGMA integrity_check`.

Перед аварийным восстановлением остановите dashboard, восстановите последнюю snapshot-копию, верните `dashboard.env` с исходным `DASHBOARD_ENCRYPTION_KEY`, затем запускайте проект. Новый ключ шифрования без миграции сделает сохранённые credentials wg-easy недоступными.
