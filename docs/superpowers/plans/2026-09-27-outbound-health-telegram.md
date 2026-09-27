# Достоверное здоровье выходов и Telegram — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:executing-plans`. Выполнять задачи с отметками `- [ ]` самостоятельно в текущей сессии, без агентов — выбор пользователя. Спецификация утверждена; этот актуализированный план ожидает проверки. Его наличие не означает, что изменения уже установлены.

**Goal:** показывать длительность действительных отказов VLESS/HY2 и передавать подтверждённые инциденты в Telegram через существующий Uptime Kuma.

**Architecture:** независимый минутный collector, сохраняемая машина состояний SQLite и отдельный минутный publisher Push API. Общий реестр позволяет позднее зарегистрировать ANTIDPI, но не изменяет два участника автоматического fallback. Контрольные пробы изолированы от рабочих health-check Mihomo.

**Tech Stack:** существующие Python 3.12, FastAPI, SQLAlchemy, APScheduler, httpx; React/TypeScript/React Query; Mihomo; Uptime Kuma 2.3.2. Без нового бот-сервиса и новых обязательных библиотек.

**Spec:** `docs/superpowers/specs/2026-09-27-antidpi-telegram-design.md`.

## Global Constraints

- ID сохраняются: `WG-IMP` → `VLESS-NL`, `HY2-USA` → `HY2-DE`; `VPS-FALLBACK` содержит только `WG-IMP`, `HY2-USA` в этом порядке.
- Цикл 60 секунд; три фиксированных HTTPS-адреса; одновременно не более трёх проб; таймаут пробы 10 секунд. Успех — минимум 2/3.
- Подтверждение отказа — 600 секунд непрерывных неудачных наблюдений; восстановление — два последовательных успешных цикла; stale — более 180 секунд.
- Ошибка controller/наблюдения не означает отказ всех VPN. Перезапуск панели не закрывает инцидент.
- Уведомления: начало, восстановление, повтор не чаще раза в час. Нет клиентских IP, посещённых URL, конфигураций или секретов.
- Не менять ключи клиентов, действующие DIRECT, роутеры, порядок/интервал production-fallback. Не отключать VPN для теста.
- Секреты — файлы `0600`, вне Git, не параметры процессов. Панель остаётся публичной с существующими auth/CSRF/SSRF-ограничениями.
- UTF-8; новые `.sh` — LF, совместимость оболочек Synology; NAS-helper — Python 3.8, PowerShell — 5.1. Нет `compose down`.
- Обслуживание не сбрасывает health-state: окна 15 минут для restart и 30 минут для update описаны в плане `2026-09-27-component-maintenance.md`. Без установленного исполнителя обслуживания окно отсутствует; этот этап от него не зависит.

## Review Focus

1. Перезапуск, скачок часов, пропуск цикла: нельзя превратить неизвестный промежуток в 600 секунд доказанного отказа. Тесты задачи 2.
2. `/delay` может менять рабочую доступность: новые контрольные пробы не должны менять выбор fallback или историю его участников. Задачи 1, 3, 6.
3. Kuma успела принять запрос, но ответ потерян: не обещать exactly-once Telegram и не устраивать серию немедленных повторов. Задача 4.
4. Collector/controller пропал, а потом появился: сообщение о возвращении мониторинга не равно восстановлению VPN. Задачи 4–6.
5. Рабочая копия уже изменена, deploy упаковывает только HEAD: нельзя потерять пользовательские изменения или выпустить неполный артефакт. Задача 6.

## Файлы и границы

Новые `backend/app/outbounds.py`, `outbound_health.py`, `health_collector.py`, `kuma_push.py`, `health_api.py` отвечают соответственно за ID, состояния, сбор, передачу, API. Существующие `models.py`, `db.py`, `settings.py`, `main.py`, `collectors.py` меняются только в точках подключения. UI использует один новый `OutboundHealthPanel.tsx`; не переписывать целиком большие страницы.

Тесты Python выполняются из `backend`: `.\.venv\Scripts\python.exe -m pytest ... -q`. Deployment-тесты из корня: `.\backend\.venv\Scripts\python.exe -m unittest discover -s deploy/tests -p 'test_*.py'`. Frontend из `frontend`: `npm run test -- --run`, затем `npm run build`.

### Задача 1: реестр выходов и безопасные диагностические группы

**Файлы:** создать `backend/app/outbounds.py`, `backend/tests/test_outbounds.py`, `deploy/scripts/configure-health-probes.py`, `deploy/tests/test_health_probe_config.py`; изменить `backend/app/settings.py`.

**Интерфейсы:** `OutboundId = Literal['WG-IMP', 'HY2-USA', 'ANTIDPI']`; `FallbackOutboundId = Literal['WG-IMP', 'HY2-USA']`; `AntidpiEngine = Literal['zapret2', 'byedpi']`. `OutboundDefinition(id, label, engine, probe_name)` — frozen dataclass. `build_outbound_registry(antidpi_engine: AntidpiEngine | None = None) -> tuple[OutboundDefinition, ...]`; `FALLBACK_MEMBERS = ('WG-IMP', 'HY2-USA')`. Helper: `configure_health_probes(text: str, outbound_ids: tuple) -> str`.

- [ ] Написать тесты: `test_registry_keeps_fallback_separate` проверяет `len(build_outbound_registry('zapret2')) == 3` и неизменный `FALLBACK_MEMBERS`; без движка выходов два. `test_probe_groups_are_single_member` проверяет `hidden: true`, `type: select`, ровно один член, `interval: 0`, `empty-fallback: REJECT`, имена `DASH-HEALTH-WG-IMP`, `DASH-HEALTH-HY2-USA`. Проверить идемпотентность, конфликт занятого имени и неизменность всех исходных секций вне добавляемых блоков.
- [ ] Запустить `pytest tests/test_outbounds.py -q` и `unittest discover -s deploy/tests -p test_health_probe_config.py`; ожидается FAIL из-за отсутствующей реализации.
- [ ] Реализовать интерфейсы и отключённую по умолчанию настройку `OUTBOUND_HEALTH_ENABLED=false`, необязательный `ANTIDPI_ENGINE`. Helper работает с ограниченными проверяемыми текстовыми якорями по существующему подходу deploy; неизвестная структура/конфликт — отказ без записи. Никакой автоматической активации или расширения `VPS-FALLBACK`.
- [ ] Повторить обе команды: PASS. В фикстуре отсутствующего участника группа не заменяется на DIRECT.
- [ ] Зафиксировать только файлы этой задачи после проверки diff; сообщение `feat: add outbound registry and isolated probe groups`. Не захватывать уже имеющиеся изменения.

### Задача 2: сохраняемое состояние и инциденты

**Файлы:** создать `backend/app/outbound_health.py`, `backend/tests/test_outbound_health.py`; изменить `backend/app/models.py`, `backend/app/db.py`, `backend/tests/test_db.py`.

**Интерфейсы:** `ControlCycle(outbound: OutboundId, cycle_id: str, completed_at: datetime, result: Literal['healthy','degraded','failed','unknown'], successes: int, selected_fallback: FallbackOutboundId | None, reasons: tuple[str, ...])`. `HealthSnapshot` содержит `outbound`, `state` (`healthy/degraded/pending/down/unknown`), `observed_at`, `pending_since`, `incident_id`, `incident_started_at`, `recovery_streak`, `last_success_at`, `last_cycle_id`. `HealthService.record_cycle(cycle: ControlCycle) -> HealthSnapshot`; `HealthService.snapshot(now: datetime) -> tuple[HealthSnapshot, ...]`.

- [ ] Написать тест `test_incident_599_600_and_two_recoveries`: при полных последовательных циклах `incident_at(599) is None`, `incident_at(600) is not None`; после одного успешного цикла ID прежний, после второго инцидент закрыт. Отдельно: один сбой из трёх → degraded, повтор cycle_id не меняет состояние, новый экземпляр сервиса с той же БД не создаёт дубль. `test_gap_and_clock_jump_are_unknown` проверяет stale 181 сек, пропуск минутного слота, время назад и вперёд; неподтверждённая серия сбрасывается, открытый инцидент сохраняется.
- [ ] Запустить `pytest tests/test_outbound_health.py tests/test_db.py -q`; ожидается FAIL новых тестов.
- [ ] Добавить таблицы `OutboundHealthState` (PK outbound), `OutboundControlCycle` (unique outbound/cycle_id), `OutboundIncident` (ID, outbound, first_failed_at, confirmed_at, recovered_at). Использовать `UtcDateTime`, атомарную SQLite-транзакцию для цикла/состояния/события. Слот цикла — UTC-минута начала; порог — время завершённых наблюдений. Разрыв последовательности сбрасывает pending и счётчик восстановления; два успеха должны быть подряд. Никаких миграций старых ID и клиентских данных. События начала/восстановления — одна запись на переход, без сырых исключений.
- [ ] Повторить тесты: PASS; миграция существующей БД аддитивна и повторяемая. Добавить проверку, что `snapshot()` отмечает stale даже при полностью остановленном scheduler.
- [ ] Scoped commit: `feat: persist outbound health incidents`.

### Задача 3: отдельные контрольные пробы с ограничением нагрузки

**Файлы:** создать `backend/app/health_collector.py`, `backend/tests/test_health_collector.py`; изменить `backend/app/mihomo.py`, `backend/app/main.py`, `backend/app/collectors.py`, `backend/tests/test_mihomo.py`, `backend/tests/test_collectors.py`.

**Интерфейсы:** `HealthCollector.run_cycle() -> bool` (False, если предыдущий ещё идёт). Потребляет реестр задачи 1 и `HealthService` задачи 2. `MihomoClient.control_delay(probe_name: str, endpoint_key: Literal['cloudflare','google','github']) -> ProxyDelay`; ключ разрешается только во внутренней фиксированной таблице URL. Для HTTP-вызова controller таймаут 12 секунд, timeout самой пробы 10000 мс.

- [ ] Написать `test_control_cycle_is_bounded_and_independent`: максимум три одновременных запроса, три пробы на зарегистрированный выход, вызовы только `DASH-HEALTH-*`, никаких PUT в fallback и никаких пользовательских URL. Фиксированные URL: `https://cp.cloudflare.com/generate_204`, `https://www.google.com/generate_204`, `https://api.github.com/`. Проверить, что редактирование списка ручных проверок не меняет контрольный набор. `test_controller_failure_is_unknown`, `test_cancellation_does_not_record_failure`, `test_duplicate_jobs_do_not_overlap` обязательны.
- [ ] Запустить `pytest tests/test_health_collector.py tests/test_mihomo.py tests/test_collectors.py -q`; ожидается FAIL новых проверок.
- [ ] Реализовать отдельный клиент/внутренний метод с собственной фиксированной allowlist, не менять пользовательскую SSRF-политику. Проверять controller и точный состав диагностических групп; отсутствие/ошибка группы — unknown, не down. После транспортных ошибок controller выполнить ограниченную повторную проверку его доступности перед классификацией. HTTPS-ответ в delay означает доступность транспорта, не вход в приложение. Сохранить результаты каждой контрольной точки и завершённый `ControlCycle`; неожиданный сбой collector — unknown. APScheduler: 60 сек, `max_instances=1`, `coalesce=True`, без «догоняющей» пачки. Ручные проверки не вызывают `HealthService.record_cycle()`.
- [ ] Повторить тесты: PASS. Пятиминутную диагностику не смешивать с новой оценкой; новый scheduler не блокирует login, UI или сбор трафика.
- [ ] Scoped commit: `feat: collect bounded independent outbound health`.

### Задача 4: безопасный publisher Uptime Kuma

**Файлы:** создать `backend/app/kuma_push.py`, `backend/tests/test_kuma_push.py`; изменить `backend/app/models.py`, `backend/app/settings.py`, `backend/app/main.py`; создать `deploy/monitoring/telegram-template.txt`.

**Интерфейсы:** `KumaPushClient.publish(token: str, *, status: Literal['up','down'], message: str) -> None`; `KumaPublisher.run_once(now: datetime) -> None`. `NotificationDeliveryState` хранит для каждого monitor key `last_attempt_at`, `last_accepted_at`, `pending_revision`, `accepted_revision`, `last_error_code`; URL и токен в таблицу не попадают. `KUMA_PUSH_TOKENS_FILE` — необязательный JSON-файл `0600` с ключами `WG-IMP`, `HY2-USA`, `collector`, позднее `ANTIDPI`; отдельный тестовый токен не использовать для production-истории.

- [ ] Написать `test_publisher_coalesces_and_never_retries_within_60_seconds`: 10 запусков/ручных проб в одну минуту дают не более одного push на monitor; после timeout повтор не раньше 60 секунд, без накопления устаревших статусов. Проверить HTTP 404/500, `{ok:false}`, разрыв ответа после принятия, restart, неготовые токены и запрет redirect. `test_unknown_preserves_incident_and_has_distinct_message` проверяет сохранение down при открытом инциденте, отдельно unknown collector, отсутствие ложного `INCIDENT_RECOVERED`. `test_secret_redaction` ищет тестовые секреты в логах, ошибках и API и не находит их.
- [ ] Запустить `pytest tests/test_kuma_push.py -q`; ожидается FAIL.
- [ ] Реализовать независимый минутный publisher и ограниченную задержку повторов 60/120/300 секунд; только актуальное состояние, без немедленного retry. Ответ считается принятым только при HTTP 200 и `ok:true`. До первого достоверного наблюдения выход не объявляется исправным. В сообщении — код события, название, first_failed_at/длительность, fallback и краткая причина из фиксированного списка. Открытый инцидент закрывается только задачей 2; сведения для сообщения восстановления читаются из последнего закрытого `OutboundIncident`, revision не позволяет выдавать каждую следующую минуту за новое восстановление. Доставка в Telegram остаётся «не подтверждена» даже после принятия Kuma. Долгая недоступность Kuma не блокирует сбор и не создаёт растущую очередь. HTTP-клиент: `trust_env=False`, проверка TLS, запрет redirects; не допускать логирования URL с push-токеном библиотекой httpx.
- [ ] Проверить шаблон plain-text для Kuma: собственные коды `INCIDENT_DOWN`/`INCIDENT_RECOVERED` отображают инцидент; автоматический timeout push-monitor — «нет данных мониторинга»; обычный up после timeout — «наблюдение возобновилось», не «VPN восстановлен». В тестовой Kuma 2.3.2: `maxretries=0`, timeout heartbeat 180 сек, минутный publisher, `resendInterval=60` heartbeat; доказать напоминание не раньше 3600 секунд модельным тестом её реального алгоритма и контролируемым тестовым монитором. Если фактическая версия ведёт себя иначе, оповещения не активировать до корректировки.
- [ ] Повторить тесты: PASS. Документировать ограничение Push API: нет idempotency key; при неоднозначной потере ответа редкий дубль Telegram возможен, exactly-once не заявлять.
- [ ] Scoped commit: `feat: publish sustained incidents to uptime kuma`.

### Задача 5: отображение достоверного состояния без ожидания controller

**Файлы:** создать `backend/app/health_api.py`, `backend/tests/test_health_api.py`, `frontend/src/components/OutboundHealthPanel.tsx`, `frontend/src/components/OutboundHealthPanel.test.tsx`; изменить `backend/app/main.py`, `backend/app/routes.py`, `backend/app/schemas.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/api/outboundLabels.ts`, `frontend/src/pages/OverviewPage.tsx`, `RoutesPage.tsx`, `JournalPage.tsx` и их существующие тесты.

**Интерфейсы:** `GET /api/health/outbounds` возвращает `{observed_at, collector_state, outbounds, delivery}` из SQLite. Каждый выход: ID, label, engine, state, observed_at, pending_since, incident_id, incident_started_at, last_success_at, successes/total; delivery — `disabled/pending/accepted/error`, last_attempt_at/last_accepted_at, безопасный код ошибки. Auth обязателен. `OutboundHealthPanel({data, isLoading, error})` — один общий компонент обзора/маршрутов.

- [ ] Написать `test_health_api_works_when_controller_is_down`: HTTP 200 с сохранённым/stale состоянием без сетевых вызовов; анонимный запрос 401. UI-тесты: «проверяем 4 мин», «недоступен с …, 12 мин», «нет данных, последняя проверка …», третья карточка только после регистрации. Проверяемый выход и выбранный fallback различаются в журнале. Нет зелёного «в норме» у неизвестного состояния. Ошибки соседней `/overview` не скрывают health-карточки.
- [ ] Запустить новые Python-тесты и `npm run test -- --run src/components/OutboundHealthPanel.test.tsx`; ожидается FAIL.
- [ ] Реализовать отдельный запрос React Query раз в 60 секунд, без polling при скрытой вкладке; возраст пересчитывать локально, не сетью каждую секунду. В журнале добавить тип `outbound_health`, не переопределять смысл старых `ProbeEvent.target` (проверяемый выход) и `ProbeEvent.outbound` (выбранный fallback). Кнопка «Проверить сейчас» остаётся диагностикой, не подтверждает/закрывает инцидент.
- [ ] Запустить `pytest tests/test_health_api.py tests/test_auth.py -q`, frontend-тесты затронутых страниц и `npm run build`: PASS. Проверить long label/ошибку/загрузку и отсутствие секретов в ответах.
- [ ] Scoped commit: `feat: show observed health and notification status`.

### Задача 6: безопасная активация, backup и приёмка

**Файлы:** создать `deploy/enable-outbound-health.ps1`, `deploy/scripts/enable-outbound-health.sh`, `deploy/tests/test_health_deployment.py`, `docs/telegram-monitoring.md`; изменить `deploy/scripts/backup-dashboard.sh`, `deploy/scripts/restore-verify.sh`, `compose.yaml`, `deploy/dashboard.env.example`.

**Интерфейсы:** wrapper без секретных аргументов запускает последовательность preflight → encrypted backup → сверка исходной revision/hash → `/mihomo -t` → reload → проверка результата. Результат машины — `not_started/validated/applied/verified/rolled_back/unknown`; обрыв SSH сам по себе не значит успешный откат. Статус хранится на NAS без секретов.

- [ ] Написать тесты `test_concurrent_config_change_aborts`, `test_disconnect_is_unknown_not_success`, `test_artifact_contains_imported_modules`, `test_backup_includes_consistent_kuma_database`. Не включать несохранённые изменения чужих задач в commit; перечислить текущий dirty diff перед работой. Упаковка HEAD должна содержать все импортируемые модули, включая ранее untracked `outboundLabels.ts`, либо выпуск блокируется до согласованного включения этих зависимостей. Не использовать общий `git add .`, stash или reset.
- [ ] Запустить `unittest discover -s deploy/tests -p test_health_deployment.py`: FAIL новых тестов. Затем реализовать helper в стиле имеющихся скриптов, SCP `-O`, явный путь Docker, без сложных вложенных кавычек SSH. Backup SQLite через online backup, включая DB Kuma с WAL; архив рабочих файлов без согласованного снимка БД недостаточен. Проверить чтение/восстановление зашифрованного архива в отдельный временный каталог, не поверх production.
- [ ] Выполнить полный backend/frontend/deploy наборы; PASS, `git diff --check` чистый. На изолированном Mihomo проверить, что тест диагностической группы меняет только её историю, не историю рабочего выхода/fallback, включая неуспех. Это gate до включения минутного collector.
- [ ] При доступном SSH и нужном sudo подготовить backup, применить только диагностические группы горячей загрузкой, не перезапускать WireGuard. Если для восстановления доступа нужны Windows-маршруты — дождаться отдельного разрешения, не считать одобрение плана таким разрешением. Убедиться, что `VPS-FALLBACK`, DNS, DIRECT и ключи не изменились. Включить collector без Telegram, проверить минимум два цикла и исходную нагрузку.
- [ ] В существующей Kuma через защищённый UI создать push-monitor на каждый установленный выход и collector. Пользователь создаёт BotFather-токен и запускает бота; токен вводится только в Kuma. Настроить шаблон задачи 4, проверить недоступность Push API извне и сохранность токенов. Отдельный тестовый monitor: начало/напоминание/восстановление и потеря heartbeat; пользователь подтверждает получение тестового Telegram. Не имитировать отказ работающих VPN.
- [ ] Проверить перезапуск только панели: нет повторного открытия инцидента/ложного восстановления, продолжение publisher. Проверить откат панели с сохранением новых аддитивных таблиц, удаление только диагностических групп, отключение новых мониторов; клиентские профили прежние. Сырые контрольные циклы хранить 30 дней, инциденты 365 дней; retention не касается WireGuard и месячных/годовых итогов.
- [ ] Зафиксировать проверенные изменения задачи и фактический протокол приёмки. В итогах отдельно указать: локальные тесты, установленное на NAS, проверенное Telegram; незавершённые live-gates не называть успехом.

## Зависимость следующего этапа

План `2026-09-27-antidpi-outbound.md` использует `OutboundDefinition`, `HealthService` и publisher из этого этапа. Telegram для двух действующих VPN может быть включён независимо от готовности NFQUEUE/anti-DPI. Интеграция окон обслуживания, отдельной истории job и уведомления о результате реализуется в задаче 5 плана `2026-09-27-component-maintenance.md`, не добавляет новую конкурирующую машину VPN-инцидентов.
