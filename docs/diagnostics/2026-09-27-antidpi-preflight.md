# AntiDPI: read-only preflight DS923+

Фактическая проверка: **28.09.2026, 09:23:27 UTC+5** (04:23:27 UTC).
Результат: **unknown — выбор движка пока не подтверждён**.

SSH работал под существующей учётной записью. Коллектор передан через stdin, без файлов на NAS, sudo, установки контейнеров, загрузки модулей, команд firewall или изменений маршрутов. Конфигурации и ключи VPN не читались.

| Проверка | Наблюдение |
| --- | --- |
| Архитектура / ядро | x86_64 / 4.4.302+ |
| Python | 3.8.15; сам коллектор успешно выполнен этой версией |
| Network namespace | `/proc/self/ns/net` доступен |
| Kernel config | `/proc/config.gz` и `/boot/config-4.4.302+` не удалось прочитать |
| `nfnetlink_queue`, `xt_NFQUEUE` | Файлы есть, vermagic `4.4.302+ SMP mod_unload`, не загружены |
| `nfnetlink`, `nf_conntrack`, `nf_conntrack_ipv4` | Загружены; файлы модулей соответствуют ядру по vermagic |
| `ip_tables`, `iptable_mangle` | Загружены; файлы модулей соответствуют ядру по vermagic |
| `xt_connmark`, `xt_owner` | В проверенных стандартных расположениях не обнаружены; поддержка не опровергнута |
| `CONFIG_NF_CONNTRACK_MARK` | Не подтверждён без kernel config |
| Docker metadata из collector | Недоступны непривилегированному пользователю |

Ранее в этой же сессии существующий разрешённый read-only helper `vpn-dashboard-status` отдельно подтвердил Docker **24.0.2**, работающий `vpn-dashboard` и отсутствие активного deploy. Это не заменяет проверку Docker kernel/platform. Restic не обнаружен в `/opt/bin/restic`, `/usr/local/bin/restic`, `/usr/bin/restic`; это также не доказывает его отсутствия в нестандартном месте.

Наличие совместимого по vermagic файла NFQUEUE — основание для дальнейшей проверки, не доказательство успешной загрузки/привязки очереди. Не найденный файл не считается доказательством отсутствия встроенной возможности. Проверка IPv6 и UDP/QUIC не выполнялась.

## Следующий gate

Получить полные root-read-only сведения о Docker, owner match и conntrack/CONNMARK. Затем **отдельно согласовать** изолированный тест привязки NFQUEUE, не меняя firewall DSM и работающие VPN. До этого не выбирать Zapret2 или ByeDPI автоматически и не устанавливать оба движка.

Повторный безопасный сбор: `deploy/diagnose-antidpi-support.ps1`. `candidate` означает только достаточные метаданные для следующего теста. Запуск самого коллектора не проверяет обработку пакетов.

Основания проверки: [Linux 4.4 Netfilter Kconfig](https://github.com/torvalds/linux/blob/v4.4/net/netfilter/Kconfig), [IPv4 Netfilter Kconfig](https://github.com/torvalds/linux/blob/v4.4/net/ipv4/netfilter/Kconfig), [libnetfilter_queue](https://www.netfilter.org/projects/libnetfilter_queue/).
