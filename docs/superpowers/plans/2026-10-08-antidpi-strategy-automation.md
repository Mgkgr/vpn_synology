# Anti-DPI Strategy Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Пользователь требует текущий checkout, без агентов и повторных согласований.

**Goal:** проверки по расписанию, ручной подбор и безопасная автосмена стратегий с управлением из панели.

**Architecture:** закрытый каталог → сохраняемые политики/наблюдения → один worker с общим maintenance-lock → безопасный snapshot/API/UI. Реальные сетевые пробы и применение отделены от решения; отсутствие проверенного runtime блокирует действие, а не имитирует успех. Использовать существующие owner grants и durable job IDs.

**Tech Stack:** Python 3.8 stdlib на NAS, SQLite, FastAPI/Pydantic, React/TypeScript, unittest/pytest/vitest.

**Spec:** `docs/superpowers/specs/2026-10-08-antidpi-strategy-automation-design.md`; прежние задачи anti-DPI 2/3/7 остаются обязательными зависимостями production-активации.

## Исполнение 08.10.2026

- [x] Закрытый каталог и сохраняемая политика; TDD, отзыв при смене каталога/runtime, лимиты и retention.
- [x] Runner/schedule и IPC-интеграция с реальным SQLite/maintenance-lock; автоматические источники недоступны браузеру.
- [x] Строгий API и существующие owner grants; проверки CSRF, роли, повторов и потерянного ответа.
- [x] Страница стратегий, явные Auto/Pinned, расписание, прогресс, причины, даты и история.
- [x] Авторская проверка без агентов; regression RED→GREEN для stale после backup, сбоя инфраструктуры между отказами, смены каталога и повторного кешированного успеха.
- [x] Финальные deploy: 255 tests, 1 existing skip; backend: 265 passed, 1 existing skip; frontend: 91 passed; build, Python 3.8 AST и Gitleaks passed. Playwright на локальных фикстурах: desktop/mobile, ввод/фокус/Escape, ошибок консоли нет.
- [x] Read-only NAS preflight: панель running, Docker24.0.2; новый socket/code не видимы текущему пользователю. Рабочие маршруты и контейнеры не изменены.
- [x] Реализованы конкретный host-адаптер, runtime, DNS-renewal и постоянный root-worker; создана отдельная пара на NAS, служба active/enabled. Это не приёмка.
- [x] После отдельного подтверждения владельца установлены четыре проверенных изменения новой службы: канонический Mounts order, ограниченные DNS-повторы, systemd219 и отсутствие лишних записей выключенного расписания. Проверенная зашифрованная копия исходников/runtime/SQLite; основные VPN не перезапускались.
- [x] Первоначальные семь просроченных снимков из 19 устранены согласованным независимым DNS-refresher: последующее десятиминутное наблюдение дало 301 замер, 57 обновлений, 0 просроченных снимков и 21 успешную проверку runtime. Это не приёмка клиентского пути или Auto; после новой identity обязательны остальные gates.
- [x] 09.10: постоянное обновление DNS, реальное истечение lease/fail-closed/recovery, восстановление namespace, смена/откат одного сервиса и 12/12 HTTPS-проб через WG-клиент. Root-owned receipts и отчёт 09.10; область только четыре контрольных HTTPS-хоста, без приложения/видео/UDP.
- [x] 09.10: установленная панель подключена к worker через read-only Unix socket; зашифрованный backup/пробное восстановление, тот же образ/UID10001, ready200/anonymous401, корректная схема четырёх сервисов. Пересоздан только dashboard.
- [ ] Включение политик владельцем и сквозная приёмка кнопок/автоматики на NAS. Auto выключен; runtime-приёмка не подменяет разрешение конкретной политики.

Инструкция и точные ограничения: [Стратегии ByeDPI](../../antidpi-strategies.md).
Ниже сохранён исходный порядок шагов; итог выполнения и незавершённые зависимости перечислены выше.

## Global Constraints

- Никакого изменения VLESS/HY2, их fallback, DIRECT, ключей WG, firewall DSM или автоматического расширения routing.
- Только четыре согласованных HTTPS-сервиса и контроль Wikipedia; TCP/443, проверенные IP, TLS, без redirect.
- 3 отказа через 30 секунд; кандидат 3/3 через 10 секунд; свежесть 180 секунд; проверка текущей перед сменой.
- Интервалы 5/15/30/60 минут, по умолчанию 30; суточный набор в 05:30 Asia/Yekaterinburg, до двух альтернатив.
- Не чаще 15 минут, максимум две смены/час; pinned не меняется; нет автоматического возврата исправной стратегии.
- Один job, устаревшая ревизия запрещена; ручной тест не применяет результат и не увеличивает automatic failure streak.
- 60 попыток/1200 секунд на ручной подбор, 10 секунд/32 KiB на запрос; частичный результат не считается приёмкой.
- Нет пользовательских shell/Lua/путей/команд. Кодировка UTF-8, Python 3.8-compatible worker.
- Не объявлять инфраструктурную ошибку отказом стратегии; не обещать приложения/UDP по HTTPS.

## Review Focus

1. Поддельный/старый результат с будущим временем, иной DNS-парой или версией не должен разрешить применение (tasks 1/2).
2. Перезапуск, потеря ответа, две вкладки или отмена не должны продублировать запись/смену (tasks 1–3).
3. Ручной подбор и фоновые GET не включают автоматическую смену обходным путём (tasks 2–4).
4. Отказ worker/runtime, HTTP-denial и отозванная политика должны быть явно видны, не зелёными (tasks 1–4).
5. Право владельца ограничено конкретной операцией и каталогом; неизвестные поля/команды и чужие профили отвергаются (tasks 1–4).

### Task 1: Closed catalog and durable strategy state

**Files:** create `deploy/maintenance/strategy_catalog.py`, `strategy_state.py`, `deploy/tests/test_strategy_state.py`; extend `protocol.py` only after tests.

**Interfaces:** `StrategyRequest` (check/tune/configure/apply/rollback, service_id, expected_revision, settings); `StrategyStore(JobStore)` stores policy, observations, change history; `snapshot(now)`, `configure(request, actor, now)`, `observe(service_id, result, source, now)`, `decision(service_id, now)`.

- [ ] Write behavioural unittest cases for one/three failures, 30-second spacing, success/unknown reset, manual isolation, finite/old/future time, compatible identity, pinned, revoked policy, persistent cooldown and two/hour, revision conflict and closed schemas.
- [ ] Run `backend/.venv/Scripts/python.exe -m unittest discover -s deploy/tests -p test_strategy_state.py`; Expected: FAIL for missing implementation.
- [ ] Implement catalog IDs and root SQLite state using existing private path validation/transactions; no network or Docker in this layer. State changes invalidate only configuration revision, not telemetry. Retention 30/90 days and 50000 probes.
- [ ] Repeat focused + full deploy suite; Expected: PASS (existing Linux-only skip allowed). Scoped commit.

### Task 2: Bounded runner, schedule and worker integration

**Files:** create `deploy/maintenance/strategy_runner.py`, `strategy_schedule.py`, `deploy/tests/test_strategy_runner.py`; extend service/protocol/store as required.

**Interfaces:** trusted adapter has `capabilities()`, `probe(service_id, strategy_id, context, timeout)`, `comparison_context(service_id)`, `apply(selections, expected_revision)`, `rollback(token)`, `verify(service_id)`; `StrategyRunner.run(job_id)`, `tick(now)`. Only host code constructs adapters. `MaintenanceService(..., strategies=None)` exposes `strategy_snapshot`, dispatches StrategyRequest into existing JobStore. Unconfigured adapter exports precise blockers.

- [ ] RED tests using actual state/jobs and deterministic transport boundary: 3 fresh successes on one DNS context, current recovery abort, rejection on identity drift/revocation/cancel; verified apply vs rollback/needs_reconcile; unsupported executor never enqueues; manual never applies.
- [ ] RED schedule tests: default interval, burst retries, 05:30 once/day, missed-run coalescing, timezone, restart/clock reversal, busy lock and bounded queue.
- [ ] Implement one decision owner and stable candidate order, persisted jobs/results/progress, shared lock and effect records before mutation; short explicit adapter timeouts and deadline checks. No guessing NAS readiness.
- [ ] Repeat focused/full deploy tests; Expected: PASS. Scoped commit with honest runtime gating.

### Task 3: Authenticated API and safe snapshots

**Files:** create `backend/app/antidpi_api.py`, `antidpi_schema.py`, `backend/tests/test_antidpi_api.py`; extend maintenance API/client/main.

**Interfaces:** `GET /api/antidpi/strategies` → catalog/services/policies/results/history/blockers/capabilities/revision; existing maintenance authorize/submit carries closed StrategyOperation. Job result/progress available by exact ID, no raw logs.

- [ ] RED HTTP tests with real auth/grants: anonymous 401; admin read-only; owner+CSRF+bound grant; malformed values and arbitrary targets rejected; retry lost submit sends IPC once; unavailable worker returns explicit read-only state.
- [ ] Implement thin router, strict safe response validation and closed operation union. Passwords/tokens never go to worker/logs; snapshot does not run probes.
- [ ] Run backend full pytest and deploy contract tests; Expected: PASS. Scoped commit.

### Task 4: Strategy UI and end-to-end local acceptance

**Files:** create `frontend/src/pages/AntidpiStrategiesPage.tsx`, tests, API types and confirmation UI; extend App/RoutesPage/client and existing job progress rendering.

**Interfaces:** `/routes/antidpi/strategies`; API from task 3; existing maintenance authorization/job status/cancel.

- [ ] RED vitest scenarios: real active/pinned vs selected draft, unavailable/stale state, timing/error labels, interval toggle, check/tune/apply/rollback, owner restrictions, unknown-response no resend, immediate cache refresh; keyboard-accessible modal.
- [ ] Implement compact per-service cards with separate current choice and editable selection, distinct Apply/Pin/Auto, real progress, recent changes and expandable candidate results. Never fake success or supported capabilities.
- [ ] Full deploy/backend/frontend suites + build, Python 3.8 AST, UTF-8 and publication scan. Expected: PASS, report any existing warning/skip.
- [ ] Author review (no agents per user); fix important findings RED→GREEN. Read-only NAS preflight; only enable production after actual dependency gates pass. If missing, record exact gate and continue all independent local work; do not mark rollout complete.
- [ ] Scoped commit, update operational report and ledger. No push or unrelated deployment implicitly added.
