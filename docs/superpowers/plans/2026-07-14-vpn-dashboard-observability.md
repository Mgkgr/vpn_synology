# VPN Dashboard Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить достоверное наблюдение GeoData/правил, безопасные пользовательские проверки маршрутов и немедленную реакцию dashboard на действия администратора.

**Architecture:** Backend остаётся единственным доверенным исполнителем: он читает metadata GeoData, сохраняет проверки и валидирует пользовательские цели до вызова Mihomo. Frontend получает типизированные DTO, обновляет локальный кеш React Query сразу после успешного действия и подтверждает операции собственными компонентами, а не браузерными диалогами.

**Tech Stack:** FastAPI, SQLAlchemy/SQLite, APScheduler, Mihomo controller API, React 19, TypeScript, TanStack Query, Vitest, Testing Library.

## Global Constraints

- Не создавать контейнеры, не менять VPN-маршрутизацию, DSM firewall или публичные порты.
- Не возвращать секреты, приватные адреса, сырой `detail` или `error_text` интеграций в API/UI.
- Пользовательские проверки принимают только публичные HTTPS hostnames на порту 443; private/loopback/link-local/reserved адреса, credentials и дубликаты запрещены.
- Сохранять существующие `WG-IMP → HY2-NL` роли и совместимость текущих маршрутов API.
- Новое поведение реализовывать через тесты, созданные и запущенные до production-кода.
- Git-коммит не выполняется: рабочая папка не является Git-репозиторием.

---

## File structure

- `backend/app/collectors.py` — read-only статус текущих GeoData и lock ручного/планового probe cycle.
- `backend/app/probe_targets.py` — persisted built-in/custom targets, URL/DNS/IP validation и CRUD service.
- `backend/app/schemas.py` — DTO для Geo assets, rule changes, безопасной причины probe и custom targets.
- `backend/app/routes.py` — новые endpoints, DTO mapping и allowlisted journal subjects.
- `backend/app/policy_rules.py` — актуальный finite MetaCubeX catalogue.
- `backend/tests/test_collectors.py`, `backend/tests/test_routes.py`, `backend/tests/test_probe_targets.py` — API/service regression tests.
- `frontend/src/api/{types.ts,client.ts}` — новые типы и запросы.
- `frontend/src/components/{ToastRegion.tsx,ConfirmDialog.tsx}` — reusable site-native feedback.
- `frontend/src/pages/{ClientsPage,RoutesPage,UpdatesPage,JournalPage,RulesPage}.tsx` — пользовательские потоки.
- `frontend/src/pages/*Page.test.tsx`, `frontend/src/api/client.test.ts`, `frontend/src/styles.css` — UI tests и стили.

## Task 1: GeoData и история изменений правил

**Files:**
- Modify: `work/vpn-dashboard/backend/app/collectors.py`
- Modify: `work/vpn-dashboard/backend/app/schemas.py`
- Modify: `work/vpn-dashboard/backend/app/routes.py`
- Modify: `work/vpn-dashboard/backend/tests/test_collectors.py`
- Modify: `work/vpn-dashboard/backend/tests/test_routes.py`

**Interfaces:**
- Produces `Collector.current_geo_metadata() -> tuple[GeoFileSnapshot, ...]` and `Collector.current_geo_metadata_error() -> str | None`.
- Produces `UpdatesResponse(assets: list[GeoAssetResponse], updates: list[GeoUpdateResponse], rule_changes: list[RuleChangeResponse])`.
- `GeoAssetResponse` contains `filename`, `kind`, `size_bytes`, `modified_at`, `sha256`, `last_observed_at`.

- [x] **Step 1: Write failing backend tests for current assets and rule history.**

```python
def test_updates_returns_current_geosite_and_geoip_metadata(route_parts, tmp_path) -> None:
    collector = client.app.state.runtime.collector
    collector._files = FakeGeoStore((
        GeoFileSnapshot("geosite.dat", 12, observed_at, "a" * 64),
        GeoFileSnapshot("geoip.dat", 10, observed_at, "b" * 64),
    ))

    payload = client.get("/api/updates").json()

    assert [(item["kind"], item["modified_at"]) for item in payload["assets"]] == [
        ("GeoIP", "2026-07-14T10:00:00Z"),
        ("GeoSite", "2026-07-14T10:00:00Z"),
    ]

def test_updates_includes_audited_direct_and_policy_changes(route_parts, factory) -> None:
    with factory.begin() as session:
        session.add_all([...direct_apply_event..., ...policy_rule_create_event...])

    assert [item["action"] for item in client.get("/api/updates").json()["rule_changes"]] == [
        "policy_rule_create", "direct_rules_apply"
    ]
```

- [x] **Step 2: Run the focused tests and verify red.**

Run: `python -m pytest tests/test_collectors.py tests/test_routes.py -k "updates_returns_current or updates_includes" -v`

Expected: FAIL because `assets` and `rule_changes` do not exist.

- [x] **Step 3: Implement the read-only metadata projection.**

Add a public collector method which calls existing `_read_geo_metadata()` only; do not write a `GeoUpdate` during page load. Map filenames case-insensitively as `GeoSite` (`geosite*`), `GeoIP` (`geoip*`), `MMDB` (`*.mmdb`, excluding ASN) and `ASN` (`*asn*`). In `/updates`, read `GeoUpdate`, `GeoFileMetadata` and only allowlisted audit action prefixes `direct_rules_` and `policy_rule_`, then map each to typed response data. `last_observed_at` comes from the newest persisted snapshot for the same filename; it remains `null` if no snapshot exists.

- [x] **Step 4: Run the focused tests and verify green.**

Run: `python -m pytest tests/test_collectors.py tests/test_routes.py -k "updates_returns_current or updates_includes" -v`

Expected: PASS.

- [x] **Step 5: Run all backend tests for this boundary.**

Run: `python -m pytest tests/test_collectors.py tests/test_routes.py -v`

Expected: PASS with no altered secret-redaction assertions.

## Task 2: Актуальный ограниченный каталог GeoSite/GeoIP и loading-state правил

**Files:**
- Modify: `work/vpn-dashboard/backend/app/policy_rules.py`
- Modify: `work/vpn-dashboard/backend/tests/test_routes.py`
- Modify: `work/vpn-dashboard/frontend/src/pages/RulesPage.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/RulesPage.test.tsx`
- Modify: `work/vpn-dashboard/frontend/src/styles.css`

**Interfaces:**
- `POLICY_CATEGORIES` exposes finite values: `category-ai-!cn`, `openai`, `google`, `github`, `youtube`, `telegram`, `microsoft`, `apple`, `netflix`, `spotify`, `twitter`, `tiktok`, `geolocation-!cn`, plus `google`, `telegram`, `cloudflare`, `cloudfront`, `netflix`, `US`, `NL` GeoIP values.
- Rules page renders `role="status"` with `Загружаем GeoSite, GeoIP и правила Mihomo…` while `/api/rules` is unresolved.

- [x] **Step 1: Write failing API and UI tests.**

```python
def test_rules_catalogue_contains_current_ai_and_service_categories(route_parts) -> None:
    categories = {(item["kind"], item["category"]) for item in client.get("/api/rules").json()["policy_catalog"]}
    assert ("GEOSITE", "category-ai-!cn") in categories
    assert ("GEOSITE", "microsoft") in categories
    assert ("GEOIP", "cloudfront") in categories
```

```tsx
it('shows an explicit rules-loading status before the catalogue is rendered', () => {
  vi.spyOn(api, 'rules').mockReturnValue(new Promise(() => {}))
  renderPage(<RulesPage />)
  expect(screen.getByRole('status')).toHaveTextContent('Загружаем GeoSite, GeoIP и правила Mihomo')
})
```

- [x] **Step 2: Run focused tests and verify red.**

Run: `python -m pytest tests/test_routes.py -k catalogue -v; npm test -- --run src/pages/RulesPage.test.tsx`

Expected: FAIL because the category and status text are absent.

- [x] **Step 3: Implement the finite catalogue and state.**

Replace the stale Anthropic-only selector with `category-ai-!cn` labelled `AI: OpenAI, Claude, Gemini и другие`; retain narrow `openai`. Add the supported service and GeoIP categories listed above, without accepting arbitrary category names. In `RulesPage`, gate the policy composer and lower panels behind one explicit loading block; only render empty states after the request resolves.

- [x] **Step 4: Run focused tests and verify green.**

Run: `python -m pytest tests/test_routes.py -k catalogue -v; npm test -- --run src/pages/RulesPage.test.tsx`

Expected: PASS.

## Task 3: Безопасные custom targets, ручной probe cycle и видимая причина ошибки

**Files:**
- Modify: `work/vpn-dashboard/backend/app/probe_targets.py`
- Modify: `work/vpn-dashboard/backend/app/collectors.py`
- Modify: `work/vpn-dashboard/backend/app/schemas.py`
- Modify: `work/vpn-dashboard/backend/app/routes.py`
- Create: `work/vpn-dashboard/backend/tests/test_probe_targets.py`
- Modify: `work/vpn-dashboard/backend/tests/test_collectors.py`
- Modify: `work/vpn-dashboard/backend/tests/test_routes.py`

**Interfaces:**
- `ProbeTargetService.create_custom(label: str, url: str) -> ProbeTargetState`, `update_custom(key: str, label: str, url: str, enabled: bool) -> ProbeTargetState`, `delete_custom(key: str) -> None`.
- `POST /api/probes/run` returns `202`; a concurrent cycle returns `409 {"detail":"probe cycle is already running"}`.
- `RouteProbeResponse` adds nullable `reason`, containing only a short safe diagnostic string.

- [ ] **Step 1: Write failing security/service/API tests.**

```python
@pytest.mark.parametrize('url', [
    'http://example.com/', 'https://127.0.0.1/', 'https://192.168.2.1/',
    'https://[::1]/', 'https://user:pass@example.com/', 'https://example.com:8443/',
])
def test_custom_probe_rejects_non_public_https_urls(service, url) -> None:
    with pytest.raises(ProbeTargetValidationError):
        service.create_custom('unsafe', url)

def test_manual_probe_is_audited_and_reports_a_safe_failure(route_parts) -> None:
    response = client.post('/api/probes/run', headers=csrf(client))
    assert response.status_code == 202
    assert client.get('/api/routes').json()['probes'][0]['reason'] == 'HTTP 404'
```

- [ ] **Step 2: Run focused backend tests and verify red.**

Run: `python -m pytest tests/test_probe_targets.py tests/test_collectors.py tests/test_routes.py -k "custom_probe or manual_probe or safe_failure" -v`

Expected: FAIL because CRUD, endpoint, lock and `reason` do not exist.

- [ ] **Step 3: Implement persisted custom target management.**

Keep existing built-ins identifiable by their non-`custom:` keys and create custom keys prefixed `custom:` with `uuid4().hex`; no schema migration is needed. Validate a hostname-only HTTPS URL with port `None`/`443`, no username/password/query fragment, and `socket.getaddrinfo` results all public according to `ipaddress.ip_address(...).is_global`. Revalidate enabled targets before each probe cycle. Mutating built-ins may only change `enabled`; custom targets can be edited/deleted. Write allowlisted audit actions containing label/key only.

- [ ] **Step 4: Implement manual execution and serialization.**

Place one `asyncio.Lock` on `Collector`; both scheduler and manual route call `run_probe_cycle()`. The route returns 409 while locked, otherwise awaits the cycle and returns 202. Preserve `MihomoIntegrationError.reason` through `_short_safe_text` into `ProbeEvent.error_text`; map it as `reason` only for route probes, never for service rows or arbitrary journal details.

- [ ] **Step 5: Run focused backend tests and verify green.**

Run: `python -m pytest tests/test_probe_targets.py tests/test_collectors.py tests/test_routes.py -k "custom_probe or manual_probe or safe_failure" -v`

Expected: PASS.

## Task 4: Журнал действий администраторов и немедленные клиенты

**Files:**
- Modify: `work/vpn-dashboard/backend/app/schemas.py`
- Modify: `work/vpn-dashboard/backend/app/routes.py`
- Modify: `work/vpn-dashboard/backend/tests/test_routes.py`
- Create: `work/vpn-dashboard/frontend/src/components/ToastRegion.tsx`
- Create: `work/vpn-dashboard/frontend/src/components/ConfirmDialog.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/ClientsPage.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/ClientsPage.test.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/JournalPage.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/JournalPage.test.tsx`
- Modify: `work/vpn-dashboard/frontend/src/api/{types.ts,client.ts}`
- Modify: `work/vpn-dashboard/frontend/src/styles.css`

**Interfaces:**
- `JournalEventResponse` adds `subject: str | None`; `actor` remains visible to UI.
- `ConfirmDialog({ title, description, confirmLabel, onConfirm, onClose, pending })` and `ToastRegion({ message, tone })` are local site UI.

- [ ] **Step 1: Write failing journal and client UI tests.**

```python
def test_journal_exposes_allowlisted_client_subject_and_login_actor(route_parts, factory) -> None:
    with factory.begin() as session:
        session.add_all([...AuditEvent(actor='admin', action='login', succeeded=True)...,
                         ...AuditEvent(actor='admin', action='client_delete', detail='{"name":"pc"}', succeeded=True)...])
    events = client.get('/api/journal').json()['events']
    assert [(event['actor'], event['subject']) for event in events] == [('admin', 'pc'), ('admin', None)]
```

```tsx
it('removes a deleted client immediately after dashboard confirmation', async () => {
  vi.spyOn(api, 'deleteClient').mockResolvedValue(undefined)
  renderPage(<ClientsPage />)
  fireEvent.click(await screen.findByRole('button', { name: 'Удалить laptop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Удалить профиль' }))
  await waitFor(() => expect(screen.queryByText('laptop')).not.toBeInTheDocument())
  expect(screen.getByRole('status')).toHaveTextContent('Профиль laptop удалён')
})
```

- [ ] **Step 2: Run focused tests and verify red.**

Run: `python -m pytest tests/test_routes.py -k journal_exposes -v; npm test -- --run src/pages/ClientsPage.test.tsx src/pages/JournalPage.test.tsx`

Expected: FAIL because the subject, modal, toast and cache update are absent.

- [ ] **Step 3: Implement safe journal projection.**

Never expose generic `AuditEvent.detail`. Parse only JSON object fields `name`, `renamed_to`, `label`, `category` from known dashboard action prefixes and render a maximum 255-character subject. Add actor and subject columns with Russian action labels (`Вход`, `Создан клиент`, `Удалён клиент`, etc.) in the page.

- [ ] **Step 4: Implement client modal, toast and query update.**

Replace `window.confirm` for disable/delete with `ConfirmDialog`. On delete success, call `queryClient.setQueryData<Client[]>(['clients'], previous => previous?.filter(...))`, show one success toast, then invalidate `clients` and `traffic-usage`. On failure do not alter cached row and show an error toast. Apply equivalent in-page feedback to create, rename, enable and disable.

- [ ] **Step 5: Run focused tests and verify green.**

Run: `python -m pytest tests/test_routes.py -k journal_exposes -v; npm test -- --run src/pages/ClientsPage.test.tsx src/pages/JournalPage.test.tsx`

Expected: PASS.

## Task 5: Маршруты и обновления UI

**Files:**
- Modify: `work/vpn-dashboard/frontend/src/api/{types.ts,client.ts}`
- Modify: `work/vpn-dashboard/frontend/src/pages/RoutesPage.tsx`
- Create: `work/vpn-dashboard/frontend/src/pages/UpdatesPage.test.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/UpdatesPage.tsx`
- Modify: `work/vpn-dashboard/frontend/src/pages/RoutesPage.test.tsx`
- Modify: `work/vpn-dashboard/frontend/src/styles.css`

**Interfaces:**
- `api.createProbeTarget`, `api.updateProbeTarget`, `api.deleteProbeTarget`, `api.runProbes` use the Task 3 routes.
- Routes inspector renders at most two newest rows by default and `<details>` contains remaining entries.

- [ ] **Step 1: Write failing UI tests.**

```tsx
it('runs probes now, renders a safe reason, and hides older checks in disclosure', async () => {
  vi.spyOn(api, 'runProbes').mockResolvedValue(undefined)
  renderPage(<RoutesPage />)
  fireEvent.click(await screen.findByRole('button', { name: 'Проверить сейчас' }))
  await waitFor(() => expect(api.runProbes).toHaveBeenCalledOnce())
  expect(screen.getByText('HTTP 404')).toBeVisible()
  expect(screen.getAllByRole('row')).toHaveLength(3)
  expect(screen.getByRole('button', { name: 'Показать всю историю проверок' })).toBeVisible()
})

it('shows per-file GeoSite and GeoIP update times and rule changes', async () => {
  vi.spyOn(api, 'updates').mockResolvedValue(fixtureWithAssetsAndRuleChanges)
  renderPage(<UpdatesPage />)
  expect(await screen.findByText('GeoSite')).toBeVisible()
  expect(screen.getByText('Последнее изменение: 14.07.2026, 15:00')).toBeVisible()
  expect(screen.getByText('Создано правило GeoSite: OpenAI / ChatGPT')).toBeVisible()
})
```

- [ ] **Step 2: Run focused UI tests and verify red.**

Run: `npm test -- --run src/pages/RoutesPage.test.tsx src/pages/UpdatesPage.test.tsx`

Expected: FAIL because the API methods, controls and views are absent.

- [ ] **Step 3: Implement routes page.**

Add a site-native target form (`Название`, `https://site.example/`), mutation feedback, edit/delete controls only for custom rows, and the `Проверить сейчас` action. In the inspector, sort probes descending by `observed_at`, display two, render status/latency/reason and place the rest under `<details><summary>Показать всю историю проверок</summary>`.

- [ ] **Step 4: Implement updates page.**

Render assets before update audit. Group equal `modified_at` values in a single `Обновлены вместе` label while retaining separate rows/names. Render `last_observed_at` as `Последняя проверка dashboard`; render `нет подтверждённого снимка` when null. Add a distinct rule-changes table with actor, action, object and time.

- [ ] **Step 5: Run focused UI tests and verify green.**

Run: `npm test -- --run src/pages/RoutesPage.test.tsx src/pages/UpdatesPage.test.tsx`

Expected: PASS.

## Task 6: Full regression, live diagnosis, deployment and rendered QA

**Files:**
- No deployment-config change is expected; `work/vpn-dashboard/compose.yaml` remains untouched unless a test proves an existing writable mount is unavailable.

- [ ] **Step 1: Run full automated suites and production frontend build.**

Run: `python -m pytest`

Run: `npm test -- --run`

Run: `npm run build`

Expected: all backend/frontend tests pass and TypeScript/Vite build exits 0.

- [ ] **Step 2: Diagnose the live Mihomo delay failure before changing configuration.**

Use the existing restricted NAS helper or a read-only dashboard diagnostic to call the same controller delay endpoint for `WG-IMP` and `HY2-NL`. Record only HTTP status and redacted error reason. Do not change exits, proxy groups or secrets in this task.

- [ ] **Step 3: Deploy the existing isolated dashboard project.**

Copy only `work/vpn-dashboard` files to `/volume1/docker/vpn-dashboard` and invoke `/usr/local/sbin/vpn-dashboard-deploy`. Confirm `vpn-dashboard-status` and `vpn-dashboard-diagnose-api` are successful.

- [ ] **Step 4: Rendered QA using the public dashboard.**

Flow under test: `/clients` → site confirmation → delete a disposable client → immediate disappearance and toast; `/routes` → custom safe target validation → `Проверить сейчас` → two recent rows/expanded history; `/updates` → visible Geo asset timestamps/rule history; `/rules` → visible loading state on uncached navigation; `/journal` → actor/action/object.

Expected: no browser system dialog, no console errors, no blank/error overlay, and endpoint/API results match rendered state.

- [ ] **Step 5: Capture fresh evidence for completion.**

Run full status/diagnostic helper after deployment and record its exit code plus targeted browser checks. Report any unresolved live Mihomo upstream failure as a separate operational finding rather than claiming it was corrected by UI work.
