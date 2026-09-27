# Отдельный anti-DPI-выход — план реализации

> Для исполнителя: использовать `superpowers:executing-plans`, работать самостоятельно в текущей сессии, без агентов. План исполняется после подтверждения пользователя. Не устанавливать одновременно оба движка «на всякий случай».

**Goal:** добавить отдельно назначаемый маршрут `ANTIDPI` для существующих WireGuard-клиентов, не меняя их профили и два работающих VPN.

**Architecture:** официальный Zapret2 + HevSocks5Server в отдельном сетевом namespace при подтверждённой поддержке NFQUEUE; иначе ByeDPI с закрытым авторизованным SOCKS-входом. Mihomo направляет сюда только явно назначенные правила. Панель и Telegram используют общий реестр и состояния предыдущего этапа.

**Tech Stack:** Docker Compose Synology, nfqws2/HevSocks5Server либо ByeDPI/Mihomo, существующие Python/FastAPI и React. Версии/commit/digest фиксируются до сборки; установщик Z2K и старый tpws не используются.

**Spec:** `docs/superpowers/specs/2026-09-27-antidpi-telegram-design.md`.

## Global Constraints

- Обязательный предыдущий этап: `docs/superpowers/plans/2026-09-27-outbound-health-telegram.md`, задачи 1–5; развёртывание Telegram не блокирует локальную разработку этого этапа.
- Новый ID `ANTIDPI`, название показывает фактический движок `Zapret2` или `ByeDPI`. Это не VPN с зарубежным IP.
- `VPS-FALLBACK` остаётся `WG-IMP → HY2-USA`; никакого автоматического переключения ANTIDPI в DIRECT/VPN.
- Отдельный namespace, без host network, Docker socket, privileged и опубликованных SOCKS-портов. Секреты `0600`, вне Git.
- Не загружать модули ядра и не менять firewall DSM автоматически. Недоступный NAS/неполные сведения — `unknown`, не доказательство несовместимости NFQUEUE.
- NFQUEUE без `queue-bypass`; при отказе обработчика новый и существующий тестовый трафик не должен тихо уйти без обработки.
- DIRECT имеет прежний приоритет. Тестовые домены согласуются отдельно; массовые назначения, замена DNS NAS и отмена ограничений UDP/443 не входят в установку.
- Текущие клиентские ключи, базы, проекты, OpenVPN и роутеры не изменяются. Новые скрипты — UTF-8, `.sh` LF; Python-helper NAS совместим с 3.8.

## Review Focus

1. «Модуль не загружен»/нет `/proc/config.gz` ошибочно принимают за отсутствие NFQUEUE: решение только по доказательствам; задача 1.
2. После рестарта соседний контейнер остался в старом namespace: старый SOCKS не принимает рабочий трафик, восстановление нового сервиса проверяется явно; задачи 2, 5.
3. Умер nfqws2, а существующее TCP-соединение продолжает DIRECT: проверяются не только healthcheck и новые соединения; задачи 2, 5.
4. Неавторизованный SOCKS, FakeIP, обход по IPv6 или DNS-петля: не обходить auth и не объявлять UDP/QUIC готовыми по наличию флага; задачи 2, 3, 5.
5. Параллельное изменение правил/конфига во время установки либо отката: сверка ревизии, остановка на конфликте, сохранение ключей и чужих изменений; задачи 3, 5.

## Файлы и интерфейсы этапа

`deploy/antidpi/` — только новые runtime-ресурсы; `deploy/scripts/` — preflight, транзакционная регистрация и проверка; `backend/app/policy_rules.py` — новая цель для существующих правил. Не добавлять обработку пакетов в dashboard и не давать ему Docker socket.

Команды локальной проверки совпадают с предыдущим планом. В инструкции развёртывания явно разделять «тесты на фикстурах», «проверка на NAS», «подтверждение на реальном клиенте».

### Задача 1: read-only preflight NAS и выбор движка

**Файлы:** создать `deploy/diagnose-antidpi-support.ps1`, `deploy/scripts/diagnose-antidpi-support.sh`, `deploy/scripts/classify-antidpi-support.py`, `deploy/tests/test_antidpi_support.py`.

**Интерфейсы:** `classify_support(evidence: dict) -> dict` возвращает `{state: 'candidate'|'unsupported'|'unknown', architecture, kernel, reasons: list[str]}`. `candidate` не означает успешную привязку очереди. Отчёт без конфигураций VPN и ключей.

- [ ] Написать `test_unloaded_module_is_not_unsupported`: доступный подходящий модуль даёт candidate, отсутствие доступа к информации — unknown. `test_incomplete_probe_is_unknown` проверяет SSH timeout, несуществующий kernel config, недоступный Docker и ошибки прав. Доказанное отсутствие нужных возможностей даёт unsupported; изменения системы в read-only сценарии отсутствуют.
- [ ] Запустить `.\backend\.venv\Scripts\python.exe -m unittest discover -s deploy/tests -p test_antidpi_support.py`; ожидается FAIL.
- [ ] Реализовать сбор `uname`, архитектуры, Docker server/kernel, доступных kernel config и module metadata, NFNETLINK/NETFILTER_NETLINK_QUEUE, нужных механизмов фильтрации и owner matching. Никаких `modprobe`, записи sysctl или iptables. Использовать существующий безопасный SCP/SSH-подход с `-O` и явным Docker PATH. Классификатор не угадывает по одному `lsmod`.
- [ ] Повторить тесты: PASS. Выполнить read-only проверку на NAS только при доступном SSH; если Windows блокирует доступ, не менять её маршруты без отдельного разрешения. Сохранить обезличенный отчёт в `docs/diagnostics/2026-09-27-antidpi-preflight.md` с фактическим временем проверки.
- [ ] Для candidate запросить отдельное согласование пробной NFQUEUE-привязки в новом namespace. При успешном тесте выбрать Zapret2; при подтверждённой несовместимости — ByeDPI; unknown остаётся gate, а не разрешением ставить любой вариант.
- [ ] Scoped commit: `feat: add read-only anti-dpi compatibility preflight`.

### Задача 2: изолированный runtime выбранного движка

**Файлы:** создать `deploy/antidpi/compose.yaml`, `versions.json`, `Dockerfile.engine`, `Dockerfile.socks`, `engine-entrypoint.sh`, `socks-entrypoint.sh`, `healthcheck.py`, `gateway-auth.yaml.example`; тесты `deploy/tests/test_antidpi_runtime.py`. Для невыбранной ветки не создавать неиспользуемые образы.

**Интерфейсы:** внутренний SOCKS `antidpi:1080`, отдельный пользователь `gateway`; секрет читается из mounted файла. `healthcheck.py` возвращает 0 только при готовом обработчике/нужном SOCKS-входе; не доказывает обход DPI сайта. `versions.json` хранит выбранный engine, commit источников и digest образов, без `latest` и секретов.

- [ ] Написать `test_compose_is_private_and_least_privilege`: нет ports/host network/privileged/socket; образы pin digest, readonly rootfs, ограниченные tmpfs/logs/PID/память. `test_auth_is_required` отвергает отсутствие/неверный пароль. `test_handler_failure_fails_closed` и `test_restart_has_no_stale_namespace` описывают интеграционный контракт на изолированных фикстурах; проверяется существующая TCP-сессия, не только открытие новой.
- [ ] Запустить `unittest discover -s deploy/tests -p test_antidpi_runtime.py`; ожидается FAIL.
- [ ] Зафиксировать реальные release/commit из официальных репозиториев и SHA256 скачанного исходника/собранного образа. Проверить лицензии и совместимость amd64 с фактическим NAS. Не запускать сторонний root-installer. Собрать только выбранный вариант.
- [ ] Для Zapret2: owner namespace — контейнер обработчика `antidpi`, SOCKS Hev — non-root UID 10002 в том же namespace, все capabilities сброшены у SOCKS; только обработчику `NET_ADMIN`/`NET_RAW`. Правила касаются исходящих пакетов UID SOCKS, не всего NAS. Очередь без bypass и без обходящего её established-правила; обработка обоих направлений/семейств проверяется по фактической стратегии. SOCKS-supervisor проверяет наличие queue consumer в своём namespace и прекращает приём/закрывает сокеты при его исчезновении. Нельзя полагаться только на `depends_on`: повторный запуск должен обнаруживать старый namespace и пересоздавать только новые anti-DPI-компоненты через scoped deploy helper.
- [ ] Для ByeDPI: движок слушает только `127.0.0.1:1081`; соседний отдельный Mihomo того же namespace принимает авторизованный SOCKS на 1080. В нём нет TUN/controller/system DNS hijack; единственный outbound — SOCKS к loopback ByeDPI, единственное MATCH-правило направляет туда, без запасного DIRECT. Использовать уже проверенную фиксированную версию Mihomo. Не выдавать ByeDPI за Zapret2. Отказ loopback-прокси должен завершать запрос ошибкой, не обходом.
- [ ] Пройти локальные тесты и контейнерную интеграцию: auth, отказ handler, перезапуск, отсутствие опубликованных портов, невозможность достичь loopback ByeDPI с соседнего контейнера. Начальные лимиты нового проекта — engine 256 MiB, SOCKS/auth 128 MiB, PID 64 на сервис, tmpfs 16 MiB; не задавать `cpus`/NanoCPUs на DSM с неподдерживаемым CFS. При OOM тест считается проваленным, лимиты корректировать по измерению, а не скрывать ошибку.
- [ ] Scoped commit: `feat: package isolated authenticated anti-dpi runtime`.

### Задача 3: регистрация в Mihomo без изменения существующих маршрутов

**Файлы:** создать `deploy/scripts/configure-antidpi.py`, `deploy/tests/test_antidpi_config.py`, `backend/tests/test_policy_rules.py`; изменить `backend/app/policy_rules.py`, `backend/app/schemas.py`, `backend/app/routes.py`, `backend/app/collectors.py`, `backend/tests/test_auth.py`, `backend/tests/test_collectors.py`.

**Интерфейсы:** `configure_antidpi(text: str, *, socks_host: str, socks_port: int, username: str, password: str) -> str`; секретные значения допускаются внутри процесса, CLI принимает только путь к файлу. Новый provider `managed-antidpi` → `managed-antidpi.txt`; действие `ANTIDPI`. `AntidpiAvailability(registered: bool, ready: bool, engine: AntidpiEngine | None)` берётся из реестра, свежего health-state и root-owned отчёта приёмки, не по произвольному пользовательскому payload. `ready` дополнительно требует разрешения назначений для проверенных image digest/хеша стратегии; зелёных общих проб до приёмки недостаточно.

- [ ] Написать `test_empty_provider_preserves_routing`: после вставки список прежних правил, DNS и поля двух VPN не изменились, добавлен ровно один `RULE-SET,managed-antidpi,ANTIDPI` после DIRECT перед VPN. `test_antidpi_never_enters_fallback`, `test_conflicting_owned_block_is_rejected`, `test_second_apply_is_noop` обязательны. В policy-тестах: новый выбор недоступен до ready; при выключенной функции и отсутствии ANTIDPI-политик обычный writer не создаёт его provider и не требует его наличия. Существующее ANTIDPI-правило сохраняется при down/unknown и не превращается в DIRECT; его можно выключить/удалить или переназначить на действующую цель.
- [ ] Запустить `unittest discover -s deploy/tests -p test_antidpi_config.py` и `pytest tests/test_policy_rules.py tests/test_auth.py tests/test_collectors.py -q`; новые тесты FAIL.
- [ ] Реализовать SOCKS5 outbound и пустой file-provider; начальное `udp: false`, включение только после отдельной сквозной проверки. Использовать диагностическую группу `DASH-HEALTH-ANTIDPI` из предыдущего плана. Конфликт имени/отсутствие якоря/повторное неэквивалентное определение — ошибка без записи. Сохранить приоритет OpenAI и все существующие DIRECT-исключения. Проверка `mihomo -t` обязательна до reload.
- [ ] Расширить только схемы, где действительно можно выбирать отдельный выход: политики, ручные пробы, диагностика. Схемы выбранного fallback и событий его переключения остаются двухэлементными. Не смешивать исторические `target` и `outbound`. При создании назначения проверять registered, готовность (разрешение назначений после приёмки и свежий healthy/degraded без открытого инцидента) и наличие provider в controller. При дальнейшем отказе не удалять политику и не делать автоматический обход. Показать известные дубли назначения одной категории и приоритет DIRECT; не заявлять, что обнаружены все пересечения вложенных GeoSite-категорий.
- [ ] Повторить тесты: PASS. Проверить атомарность существующего managed rules writer, rollback при отказе reload и отсутствие автозамены любого старого ID.
- [ ] Scoped commit: `feat: add explicit anti-dpi routing policies`.

### Задача 4: третий маршрут в интерфейсе и наблюдении

**Файлы:** изменить `frontend/src/api/types.ts`, `frontend/src/api/outboundLabels.ts`, `frontend/src/components/OutboundHealthPanel.tsx`, `frontend/src/pages/RoutesPage.tsx`, `frontend/src/pages/RulesPage.tsx`, `frontend/src/pages/JournalPage.tsx` и соответствующие тесты; изменить `backend/app/settings.py`, `backend/app/main.py`, `backend/tests/test_health_collector.py`, `backend/tests/test_kuma_push.py`, `compose.yaml` (readonly mount отчёта приёмки).

**Интерфейсы:** `ANTIDPI_ENGINE` включает третью запись реестра только после проверенной регистрации; `KUMA_PUSH_TOKENS_FILE['ANTIDPI']` подключает новый monitor. `ANTIDPI_ACCEPTANCE_PATH` указывает на readonly JSON со схемой `{engine, image_digest, strategy_sha256, verified_at, policies_enabled, udp_verified}`; нет файла/не совпадает текущая ревизия — новые назначения запрещены, UDP не считается проверенным. UI использует `OutboundDefinition`, `HealthSnapshot`, delivery предыдущего плана; новый fallback-selector не создаётся.

- [ ] Написать `test_third_route_is_separate_from_fallback`: три карточки и два участника fallback; выбор карточки/«Проверить сейчас» не отправляет команд переключения. `test_rules_keep_failed_antidpi_assignment` показывает существующее назначение с предупреждением и запрещает новое до ready. `test_antidpi_target_failure_is_not_global_outage` отделяет ошибку выбранного сайта от общей транспортной доступности. Проверить оба названия движка и подпись «выход через домашнего провайдера, не зарубежный VPN».
- [ ] Запустить затронутые frontend-тесты и новые backend-тесты; ожидается FAIL новых сценариев.
- [ ] Реализовать третий вариант действий правил и отдельную карточку маршрутов. Ошибки назначения показываются на странице, без системных confirm и скрытого успешного результата. Для целевых доменов использовать существующий проверяемый механизм ручных проб; не расширять allowlist/SSRF глобально. «UDP/QUIC проверен» появляется только после сохранённого отчёта сквозной проверки, а не по `udp: true` или зелёному TCP-тесту.
- [ ] Повторить тесты, `npm run build`: PASS. В publisher третий выход обрабатывается без нового special-case цикла/таймеров; до регистрации не создаётся пустой или постоянно красный monitor. Обзор/журнал отображают transport status отдельно от работы конкретного приложения.
- [ ] Scoped commit: `feat: expose independent anti-dpi route and health`.

### Задача 5: точечное включение, проверка после рестарта и откат

**Файлы:** создать `deploy/deploy-antidpi.ps1`, `deploy/scripts/deploy-antidpi.sh`, `deploy/scripts/verify-antidpi.py`, `deploy/scripts/reconcile-antidpi.py`, `deploy/tests/test_antidpi_deployment.py`, `docs/antidpi-operations.md`; подключить новые файлы к существующему зашифрованному backup без изменения его клиентских данных.

**Интерфейсы:** deploy поддерживает `Stage` = `Preflight/Runtime/Register/Verify/Rollback`, не запускает следующие стадии при неподтверждённой предыдущей. Каждая стадия хранит run ID, хеш исходных/применённых файлов и безопасный статус на NAS. `verify-antidpi.py` использует только согласованные домены и выводит транспорт, применившееся правило/chain, время и ограниченный результат без конфигураций/ключей.

- [ ] Написать `test_stage_order_and_revision_conflict`, `test_rollback_does_not_overwrite_newer_rules`, `test_ssh_disconnect_requires_status_check`, `test_rollback_keeps_wireguard_identity`. `test_reconciler_is_scoped_and_rate_limited` проверяет отказ при несовпадающих Compose labels, отсутствие действий для совпадающих namespace, lock и пять минут между ремонтами. Проверить, что rollback отказывается удалять новый provider при появившихся после установки пользовательских правилах до их явного разбора; нет restore всего проекта поверх новых изменений.
- [ ] Запустить `unittest discover -s deploy/tests -p test_antidpi_deployment.py`; ожидается FAIL. Реализовать стадии с проверкой хешей и существующей root-границей. Никаких секретов в command line/консоли, общего `compose down`, `--remove-orphans` для чужого проекта или изменения маршрутов Windows. Reload основного Mihomo только после encrypted backup, успешного `-t` и проверки исходной ревизии; обрыв SSH → проверить сохранённый статус, не повторять вслепую.
- [ ] Выполнить полный набор backend/frontend/deploy тестов и `git diff --check`: PASS. Проверить состав артефакта и сохранность pre-existing dirty changes как в предыдущем плане.
- [ ] На NAS после разрешённого preflight запустить только новый runtime без пользовательских правил. Проверить auth, закрытые LAN/WAN порты, независимый namespace, controller обоих прежних VPN и baseline CPU/RAM/restarts. Проверить DNS/FakeIP: адрес `198.18.0.0/15` не уходит как реальное назначение; резолвинг не зацикливается через ANTIDPI. Проверить IPv6 отдельно: если не поддержан всей цепочкой, явно отвергать его в новом маршруте, не пропускать мимо обработки.
- [ ] Согласовать тестовый домен, назначить только его через ANTIDPI. Проверить с одного WireGuard-клиента DNS, TLS и реальный сервис; сравнить с DIRECT и двумя VPN без их отключения. Если стратегия не помогает на этом провайдере, зафиксировать неуспех и не объявлять готовность по общему healthcheck. Подбор стратегии ограничить новым namespace/тестовым доменом.
- [ ] На этом тестовом трафике остановить только новый обработчик: новое соединение ошибочно не проходит DIRECT, существующий поток также не продолжает обход. Восстановить только новый проект, проверить startup-порядок и отсутствие старого namespace. `reconcile-antidpi.py` сверяет network namespace PID контейнеров только своего Compose project и при несовпадении переподключает только SOCKS-компонент; фиксированные имена/labels проверяются до действия. Добавить scoped задачу DSM при загрузке и раз в минуту, без Docker socket в контейнерах. Lock, не более одной попытки исправления за пять минут, без рестарта исправного сервиса; права на helper только root. Проверить её приёмкой, не считать `depends_on` гарантией восстановления. Перезапуск всего NAS, WireGuard или основного Mihomo не нужен. Разрешённый UDP/QUIC тестировать отдельно; старые UDP/443-ограничения клиента не снимать для получения «зелёного» результата.
- [ ] После успешного теста зарегистрировать ready, включить возможность новых ANTIDPI-назначений и добавить monitor Kuma. Провести 30-минутное наблюдение малыми контрольными запросами; ограниченный потоковый тест не более 20 MiB на согласованном ресурсе. Зафиксировать CPU/RAM, OOM/restarts и влияние на действующие VPN. Подтвердить, что ключи/профили и действующие DIRECT не изменились.
- [ ] Проверить восстановление encrypted backup в отдельное место и scoped rollback: сначала разобрать новые пользовательские назначения, вернуть только свои проверенные изменения, отключить новые monitor/runtime. Не откатывать посторонние изменения администратора. Сохранить итоговый отчёт с фактическими версиями, движком и проверенными возможностями; scoped commit `feat: add guarded anti-dpi deployment and rollback`.

## Что требует участия пользователя

Доступ к NAS при действующей маршрутизации, sudo при необходимости, отдельное разрешение пробного NFQUEUE-запуска, выбор тестового сервиса и подтверждение результата на клиенте. Токен Telegram настраивается по предыдущему плану. Не просить снова присылать уже полученные VPN-ключи.

## Критерий завершения

Недостаточно запущенного контейнера. Нужны проверенный целевой сервис, отсутствие обхода при отказе, сохранность двух VPN/DIRECT/клиентских ключей, правильное состояние в панели, проверка restart/rollback и честный отчёт об UDP/QUIC. Если gate не пройден, соответствующая стадия остаётся невыполненной.
