# Root-диагностика и подготовка резервирования — 28.09.2026

Пользователь разрешил открыть SSH, самостоятельно ввёл sudo-пароль и затем разрешил продолжить подготовленные действия при успешной проверке. Использована эта же интерактивная сессия. Новых правил sudo/NOPASSWD не создавалось. Подтверждение в SSH не означает, что новый экран подтверждения владельца уже опубликован в панели.

## Проверено на NAS

- DS923+, x86_64, Linux 4.4.302+, Python 3.8.15.
- Docker 24.0.2; Compose `2.20.1-6047-g6817716`.
- Все пять ожидаемых контейнеров принадлежат правильным Compose project/service и запущены. Dashboard, WireGuard и Kuma имеют Docker health `healthy`; у Mihomo и MetaCubeXD Docker healthcheck не определён, поэтому `running` не подменяется доказательством полной работоспособности.
- OOMKilled=false; RestartCount=0 у всех пяти контейнеров на момент проверки.
- WireGuard и Mihomo имеют одинаковый фактический network namespace; ссылка Mihomo указывает на текущий ID WireGuard.
- Свободно около 2.59 TB на `/volume1` (десятичные единицы).
- Схемы трёх SQLite-БД прочитаны через `mode=ro`/`query_only`. Значения записей, приватные ключи, переменные окружения контейнеров и необработанные ошибки в отчёт не экспортировались. Это discovery структуры, не полная проверка целостности/восстановления БД.

| Компонент | Фактическая версия | Persistence |
| --- | --- | --- |
| WireGuard / wg-easy | 15.2.2 | `/volume1/docker/vpn-gateway/wireguard/data/wg-easy.db`, `wg0.conf` |
| Mihomo | v1.19.28 | `/volume1/docker/vpn-gateway/mihomo/config.yaml`, `rules/`; `cache.db` не SQLite |
| Uptime Kuma | 2.3.2 | `/volume1/docker/vpn-gateway/uptime-kuma/kuma.db`, `db-config.json` |
| Dashboard | локальный image ID, без придуманного upstream release | `/volume1/docker/vpn-dashboard/deploy/data/dashboard.sqlite3`, `deploy/dashboard.env`, два файла в `deploy/secrets/` |
| MetaCubeXD | фиксированный registry digest; версия приложения отдельно не определялась | постоянного bind-mount нет |

В wg-easy подтверждены таблицы `interfaces_table`, `clients_table`, `users_table`, `general_table`, `hooks_table`, `user_configs_table`, `one_time_links_table`, `__drizzle_migrations`. Следовательно, резервировать только `wg0.conf` недостаточно: должны сохраняться также БД с клиентами/серверной идентичностью и связанные настройки.

Dashboard использует БД около 300 MB. Её нельзя копировать как обычный файл при работающем приложении: подготовленный код использует online SQLite backup с учётом WAL. Формат production manifest и его ревизии ещё не установлен; диагностические хеши схем не являются готовым manifest.

## Telegram / AntiDPI

- В Kuma два монитора и **ноль** notification-записей. Telegram не настроен в Kuma; доставка не испытывалась. Токен нужно вводить в защищённую настройку Kuma, не в Git, argv или отчёт.
- Установленный штатный Telegram provider Kuma 2.3.2 имеет SHA-256 `ab78d3c8aaecbb280b784be2003825d9c49f706e46e11a0d30423bcffab8567e`.
- Штатные `nfnetlink_queue.ko` и `xt_NFQUEUE.ko` существуют и имеют vermagic текущего ядра, но не загружены.
- Поиск в `/lib/modules` и `/usr/lib/modules`, списки доступных iptables matches/targets и root-доступ не подтвердили `owner`, `CONNMARK`, `CONFIG_NF_CONNTRACK_MARK`. Kernel config по прежним путям недоступен. **Результат по-прежнему unknown, не unsupported.**
- Модули не загружались; очередь NFQUEUE не привязывалась; firewall DSM и контейнеров не менялся. Ни Zapret2, ни ByeDPI автоматически не выбран и не установлен. Для следующей проверки требуется отдельно оговорить изолированный тест и возможную загрузку только штатных модулей ядра.

## Выполненное изменение: Restic 0.19.1

После диагностики выполнен безопасный подготовительный этап, не зависящий от выбора AntiDPI: установлен **только бинарный Restic** в `/usr/local/bin/restic`, root:root, `0755`.

Происхождение:

- [Фиксированный официальный релиз v0.19.1](https://github.com/restic/restic/releases/tag/v0.19.1), не draft/prerelease; опубликован 05.07.2026.
- Официальная [инструкция проверки подписи](https://restic.readthedocs.io/en/stable/020_installation.html#stable-releases).
- PGP fingerprint сверён с указанным в официальной документации: `CF8F18F2844575973F79D4E191A6868BD3F7A907`. `SHA256SUMS.asc` дал VALIDSIG именно этого ключа. Ключ импортировался только в отдельный локальный диагностический GPG-каталог, не в основной keyring пользователя.
- Архив `restic_0.19.1_linux_amd64.bz2`: SHA-256 `f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c`.
- Распакованный и установленный ELF: SHA-256 `20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639`.
- NAS реально выполнил `restic 0.19.1 compiled with go1.26.4 on linux/amd64`. После установки хеш и root:root/0755 повторно проверены по SSH.

Установщик `deploy/scripts/install-pinned-restic.py` не скачивает файлы, не принимает аргументы, не перезаписывает существующую установку. Проверяет digest до выполнения, ELF/архитектуру и версию; исполняет закрытую проверенную копию и публикует её атомарно без замены существующего пути. Нет `self-update`, инициализации репозитория, пароля или удаления старых архивов.

**Зашифрованный репозиторий не создавался, backup/restore этой реализацией ещё не выполнен.** Наличие установленного Restic не закрывает gate проверяемого резервного копирования.

## Проверка после изменения

Повторный root-read-only снимок подтвердил у всех пяти контейнеров:

- `running=true`;
- тот же container ID;
- прежние StartedAt и RestartCount;
- прежний общий namespace WireGuard/Mihomo.

Mihomo не перезагружался, текущие VPN и правила не менялись. Root-worker, socket, новые задания DSM и anti-DPI не установлены. SSH-консоль оставлена открытой по договорённости; команды больше не выполняются в фоне.

## Локальная проверка

- Новый установщик: 4 теста RED → GREEN, включая неверный digest/версию, запрет перезаписи и изменение исходного файла после создания проверенной копии.
- Полный deploy suite: 133 теста, 132 passed / 1 Linux-root skip.
- Backend: 211 passed / 1 skipped, прежнее предупреждение Starlette/anyio.
- Frontend: 54 passed. Первый запуск был заблокирован sandbox (`EPERM`), повторный разрешённый запуск прошёл; ошибки приложения не скрывались.
- Дополнительно 6 тестов разового collector: отсутствие секретов в отчёте, отказ чужому Compose-проекту, отсутствие создания missing DB, чтение актуальной WAL-схемы без изменения DB/WAL, учёт extensionless secret-файлов по метаданным.
- Python 3.8 AST, UTF-8 без BOM и LF проверены для установщика.

Дальше: собрать reviewed backup/runtime manifest из фактических источников, завершить host-side probes/исполнитель и проверить восстановление отдельно; затем включать изменения компонентов. Telegram требует настройки пользователем, а AntiDPI остаётся отдельным compatibility gate.
