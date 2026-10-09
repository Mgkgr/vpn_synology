# Profile Link Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Текущий checkout, без агентов и повторных согласований по указанию пользователя.

**Goal:** менять основной VLESS и резервный Hysteria2 из панели по типовой ссылке.
**Architecture:** закрытый parser/private draft → существующий durable worker/lock → owner-bound API → форма в маршрутах. Существующий проверенный CLI используется для немедленной замены присланного ключа отдельно от разработки UI.
**Tech Stack:** Python3.8 stdlib worker, SQLite, FastAPI/Pydantic, React/TypeScript.
**Spec:** `docs/superpowers/specs/2026-10-09-profile-link-import-design.md`.

## Global Constraints

- Только VLESS TCP/REALITY/Vision→WG-IMP и HY2/salamander→HY2-USA; TLS обязателен.
- Worker не получает пароль владельца, jobs/intents не получают ссылку/секрет.
- Черновики root0700/0600, TTL1800s, максимум8; проверка действительна300s.
- Предварительные пробы3×3 с минимум2/3 у каждого; после reload2×3 все успешны.
- Тот же образ, без публикации тестового proxy, TUN, DNS, NET_ADMIN или изменения firewall.
- Основные контейнеры не перезапускаются; при неопределённом применении нет повтора.
- UTF-8, Python3.8 compatibility, no agents; проверка авторская.

## Review Focus

1. Дубликаты query, escaped Telegram URI, private/FakeIP endpoint, unknown options — task1.
2. Секрет в exception, snapshot, intent/job и чужой/истёкший draft — tasks1/3.
3. Две вкладки/потеря ответа/restart не применяют дважды — tasks2/3/4.
4. Старая успешная проба/изменённая ревизия/неверный backup блокируют замену — task2.
5. Reload применился, но ответ потерян/rollback не подтверждён — needs_reconcile, task2.

## Task 1 — parser, private draft and closed operations

Files: `deploy/maintenance/profile_links.py`, `profile_state.py`, `profile_catalog.py`;
extend `protocol.py`, tests `deploy/tests/test_profile_links.py`, `test_profile_state.py`.
Interfaces: `parse_link(str)->dict`, `safe_summary(profile)->dict`;
`ProfileRequest(action,draft_id,expected_revision)`; `ProfileDrafts.stage(uri,actor,revision,now)`,
`load(id,actor,now)`, `checked(id,revision,results,now)`, `public(id,actor,now)`.

- [ ] Tests first: valid escaped VLESS/HY2, exact fields/secret-free errors, duplicates/unknown/nonpublic rejection; TTL1800/futureclock/8draft cap, actor, mode, opaque request IDs and no secrets in DB.
- [ ] Run unittest profile tests → expected missing implementation RED.
- [ ] Implement closed schema, strict parser and protected files; integrate request parsing.
- [ ] Run focused/full deploy suite → PASS; scoped commit.

## Task 2 — bounded profile jobs and real host adapter

Files: `deploy/maintenance/profile_runner.py`, `profile_host.py`; extend `service.py`,
`worker.py`, `host_service.py`, `store.py`; tests profile runner/host/IPC.
Interfaces: `ProfileRunner(jobs,drafts,adapter).stage/snapshot/submit/job/run`;
adapter `revision`, `preflight`, `backup`, `apply`, `verify`, `rollback` with fixed paths/images.

- [ ] RED tests: no production mutation on check/failed gate; fresh evidence/backup/CAS order;
  repeated job/restart, rollback and needs_reconcile; only matching profile changes.
- [ ] Real disposable pinned-image proxy with bounded fixed HTTPS probes; existing main wrapper validation;
  encrypted backup and owned-change rollback. Persist only safe progress and opaque recovery IDs.
- [ ] Integrate existing single-job loop; GET reads cached/safe state, not probes.
- [ ] Full deploy tests, Python3.8 AST and publication scan → PASS; scoped commit.

## Task 3 — authenticated API

Files: `backend/app/profile_api.py`, `profile_schema.py`; extend maintenance API/main;
tests `backend/tests/test_profile_api.py`.
Interfaces: owner+CSRF `POST /api/outbound-profiles/preview` accepts SecretStr URI,
safe `GET /api/outbound-profiles`; profile operations reuse `/api/maintenance/authorize/jobs`.

- [ ] RED real-auth tests anonymous401/admin403/CSRF, strict response, no secret echo on422,
  no raw URI in maintenance intent, duplicate/lost-response queries previous ID.
- [ ] Implement thin API and response validation, no network probe from GET.
- [ ] Full backend/deploy tests → PASS; scoped commit.

## Task 4 — form, NAS rollout and acceptance

Files: `frontend/src/pages/OutboundProfilesPage.tsx`, confirmation, API types/client,
App/RoutesPage links, focused tests; operational docs.

- [ ] RED tests for paste→preview→check→apply, clearing URI, owner-only controls,
  stale/failed tests disable apply, lost response no resubmit, accessible confirmation.
- [ ] Implement clear preview/progress/per-target results; use existing durable job UI.
- [ ] Full tests/build, rendered desktop/mobile flow with safe fixtures → PASS.
- [ ] Encrypted backup; install scoped worker code and dashboard. Preserve Anti-DPI selections;
  new worker identity requires truthful acceptance, not automatic transfer of readiness.
- [ ] Verify actual NAS profile replacement and UI/API; push only secret-scanned code.
