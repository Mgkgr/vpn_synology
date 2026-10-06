# Daily service probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. По требованию пользователя — без агентов, в текущем checkout; не коммитить смешанный dirty tree автоматически.

**Goal:** Показать на плитках правил честные суточные HTTPS-замеры трёх выходов, не влияющие на fallback.

**Architecture:** Фиксированный каталог → три изолированные singleton-select группы Mihomo → последовательный фоновый сбор → SQLite → read-only API/плитки. Config-transform готовится отдельно; планировщик выключен до NAS-приёмки.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, APScheduler, httpx, React/TypeScript, Vitest. Новые runtime-зависимости не нужны.

**Spec:** [2026-10-04-daily-service-probes-design.md](../specs/2026-10-04-daily-service-probes-design.md)

## Global Constraints

- Часовой пояс `Asia/Yekaterinburg`, 04:30; один catch-up текущей даты, не повторять после рестарта.
- Один сетевой тест одновременно; таймауты 10/12 секунд; пачка максимум 25 минут; история 30 дней; stale 25 часов.
- Только фиксированные HTTPS URL без credentials/query/fragment; никакого пользовательского URL из API/DIRECT.
- Только `DASH-SITE-DIRECT`, `DASH-SITE-WG-IMP`, `DASH-SITE-HY2-USA`; рабочие выходы и `DASH-HEALTH-*` не проверять этим методом.
- Никаких live SSH-изменений, reload, рестарта, отключения VPN, Git push или публикации секретов в локальном этапе.

## Review Focus

- Повторный старт после 04:30/смена времени/два процесса не должны удваивать нагрузку → Task 2.
- Успех `/delay` сам по себе не подтверждает допустимый HTTP-статус: проверить свежий per-URL результат собственного wrapper → Task 1.
- Ошибка API либо неверный wrapper не должны выглядеть как авария выхода или менять fallback → Task 1/2.
- Обновление страницы/раскрытие плитки не должно запускать сетевую проверку или скрывать редактор при отказе БД → Task 3.
- Широкая категория, старый результат, частичный прогон и изменившаяся подпись выхода не должны получить ложное «работает сейчас» → Task 2/3.

### Task 1: Каталог, изолированный транспорт и provisioning transform

**Files:** create `backend/app/site_probe_catalog.py`, `deploy/scripts/configure-site-probes.py`; modify `backend/app/mihomo.py`; tests `backend/tests/test_site_probe_transport.py`, `deploy/tests/test_site_probe_config.py`.

**Interfaces:** `SiteProbeTarget(key: str, category: str, url: str)`; `SITE_PROBE_TARGETS`, `SITE_PROBE_ROUTES`; `MihomoClient.site_probe(route_id: str, service_key: str) -> SiteProbeObservation(state: str, delay_ms: int | None, reason: str | None)`; pure `configure_site_probes(text: str) -> str`.

- [x] Добавить отрицательные тесты: raw URL/имя рабочего выхода запрещены; невалидный singleton/пустой DNS/частный IP/FakeIP не запускают HEAD; HTTP 503 контроллера отличается от недоступности API; старый per-URL history не принимается за новый; нет PUT, reload, fallback-member `/delay`.
- [x] Запустить тесты и подтвердить FAIL до кода.
- [x] Реализовать каталог точных сервисов из spec с проверенными официальными HTTPS-адресами, безопасный GET-only метод и проверку свежего результата wrapper. Не угадывать точный HTTP-код/TLS/DNS-ошибку, если API их не отдаёт.
- [x] Реализовать идемпотентный узкий transform: ровно три скрытые группы, interval 0, empty-fallback REJECT; отказ при коллизиях/изменённых markers/неизвестном layout. Существующие правила, DNS, proxy secrets и fallback остаются byte-for-byte вне собственного блока.
- [x] Выполнить `pytest backend/tests/test_site_probe_transport.py -q` и `python -m unittest discover -s deploy/tests -p test_site_probe_config.py`; проверить diff самостоятельно без агентов. Не применять на NAS.

### Task 2: Персистентный сбор и read-only API

**Files:** create `backend/app/site_probes.py`, `backend/app/site_probe_api.py`; modify `backend/app/models.py`, `backend/app/settings.py`, `backend/app/main.py`, `backend/app/routes.py` (runtime field only), `deploy/dashboard.env.example`; tests `backend/tests/test_site_probes.py`, `backend/tests/test_site_probe_api.py`.

**Interfaces:** `SiteProbeService(session_factory).claim_day(now)`, `.record(...)`, `.snapshot(now)`; `SiteProbeCollector(mihomo, service, now=...).run_due()`; `register_site_probe_jobs(scheduler, collector)`; `GET /api/rules/service-checks -> {enabled, timezone, next_run_at, run, services}`.

- [x] Тесты времени: до 04:30 нет запросов; после 04:30 один claim; повторный процесс/рестарт/обратный скачок часов не повторяет дату; пропущенные прошлые дни не догоняются. Провал/отмена оставляет `interrupted`, а не фиктивный success.
- [x] Тесты хранения/API: nullable delay, отдельные observation timestamps/labels, частичные результаты, retention 30 дней, stale после 25 часов; неавторизованный доступ запрещён; GET не вызывает Mihomo; сбой контроллера даёт unknown без изменения health/audit outage.
- [x] Подтвердить FAIL; добавить таблицы run/result с уникальным ключом local date и атомарным claim, sequential collector с общей границей 25 минут, безопасные reason codes и DB-only ответ. Флаг `site_probes_enabled=False` до приёмки; disabled явно виден.
- [x] Проверить `pytest backend/tests/test_site_probes.py backend/tests/test_site_probe_api.py backend/tests/test_settings.py -q`; затем весь backend. Проверить интеграцию с lifespan без дополнительного процесса и зависимости от открытия браузера.

### Task 3: Компактные результаты в плитках

**Files:** create `frontend/src/features/rules/ServiceProbeStatus.tsx`; modify `frontend/src/features/rules/CategoryRules.tsx`, `frontend/src/pages/RulesPage.tsx`, `frontend/src/pages/RulesPage.css`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`; tests `frontend/src/pages/RulesWorkspace.test.tsx` and new component tests.

**Interfaces:** `ServiceProbeStatus({service, run, enabled})` consumes only `/api/rules/service-checks`; route labels/observed_at come from snapshot, no invented client-derived latency.

- [x] Тесты: три результата/миллисекунды/дата; stale/unknown/disabled; раскрытие точного URL/reason; общая категория без сайта; ошибка этого API не блокирует выбор правила; GET-only polling без probe POST; нет «всё приложение работает» по HEAD.
- [x] Подтвердить FAIL; добавить компактный вывод и раскрываемые детали без второй нижней таблицы. Один query на страницу, обновление не чаще минуты, в скрытой вкладке отключено.
- [x] Vitest + production build; Playwright desktop 1366×900 и mobile 390×844 на fixtures: рендер, dropdown, stale/partial error, focus и отсутствие overflow/console errors. Без новых рендеров-макетов и imagegen.

### Task 4: Общая проверка и передача к NAS-приёмке

**Files:** update `docs/operations.md`, `docs/diagnostics/2026-10-04-outstanding-work.md`, add dated implementation evidence.

- [x] Полный backend, deploy unittest, frontend Vitest/build, проверка UTF-8/diff и `deploy/check-git-publication.py`.
- [x] Авторское ревью cross-layer: нет вызовов на реальные `/delay`, нет неограниченного URL, нет автоматической установки/reload; описать ограничения HEAD/DNS и отсутствие независимого ревью.
- [x] Зафиксировать local-ready отдельно от not-deployed. Для NAS оставить checklist: свежий локальный encrypted backup, config CAS/-t, isolated wrapper-history validation, согласованный reload, включение флага, первый прогон. Не выдавать локальные тесты за production-приёмку.
- [x] Commit делать только после выбора согласованного набора файлов из dirty tree; push не входит в эту работу.
