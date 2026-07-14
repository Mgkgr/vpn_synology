# VPN Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a LAN-only dashboard that manages WireGuard profiles through wg-easy, visualizes the primary `WG-IMP` and reserve `HY2-NL` routes, and retains a year of VPN health and traffic history.

**Architecture:** Deploy a separate `vpn-dashboard` Container Manager project so no current gateway containers need recreation. One Python/FastAPI container serves a React single-page app, owns a SQLite history database, and adapts local Mihomo/wg-easy APIs; browser clients never receive controller or integration secrets. The dashboard connects to the existing external Docker network `vpn-gateway_default` and is published only as `192.168.2.103:8088`.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLAlchemy/SQLite, APScheduler, Argon2, React 19, Vite, TypeScript, TanStack Query, Recharts, Vitest, pytest.

## Global Constraints

- UI copy is Russian; no API response, browser log or audit record contains a VPN key or controller secret.
- The only active exit is `WG-IMP`; `HY2-NL` is a reserve fallback, never a sequential next hop.
- Bind the dashboard only to `192.168.2.103:8088`; do not add WAN port forwarding.
- Do not mount `/var/run/docker.sock` or grant dashboard sudo/Docker CLI access.
- Pin wg-easy to the deployed `v15.2.2` contract; disable profile mutations if its API contract does not match.
- Preserve existing gateway projects and containers; deploy dashboard as `/volume1/docker/vpn-dashboard`.
- Retain raw events for 90 days, hourly snapshots for 12 months, and monthly summaries until manual deletion.
- No git repository exists in this workspace; each task is verified but has no commit step.

---

## File Structure

```text
work/vpn-dashboard/
  Dockerfile                         # multi-stage React + Python image
  compose.yaml                       # separate Container Manager project
  backend/
    pyproject.toml                   # locked runtime/test dependencies
    app/
      main.py                        # FastAPI creation, static assets, lifespan
      settings.py                    # validated non-secret settings
      db.py                          # SQLite engine and migrations
      models.py                      # SQLAlchemy persistence models
      schemas.py                     # API request/response models
      auth.py                        # bootstrap/login/session handling
      mihomo.py                      # authenticated Mihomo controller adapter
      wgeasy.py                      # isolated v15.2.2 adapter and contract probe
      collectors.py                  # one-minute and five-minute jobs
      rules.py                       # DIRECT revisions and controller reload
      routes.py                      # dashboard REST endpoints
      services.py                    # endpoint health probes
    tests/
      test_auth.py
      test_mihomo.py
      test_wgeasy.py
      test_collectors.py
      test_rules.py
      test_routes.py
  frontend/
    package.json
    src/
      app.tsx
      api.ts
      types.ts
      styles.css
      pages/OverviewPage.tsx
      pages/RoutesPage.tsx
      pages/ClientsPage.tsx
      pages/RulesPage.tsx
      pages/UpdatesPage.tsx
      pages/JournalPage.tsx
      components/AppShell.tsx
      components/ServiceTable.tsx
      components/RouteBranches.tsx
      components/ClientTable.tsx
      components/TrafficChart.tsx
      components/ProbeTable.tsx
      components/GeoStatus.tsx
      components/ProfileDialog.tsx
      components/RuleEditor.tsx
    src/**/*.test.tsx
  deploy/
    dashboard.env.example            # names only, no secrets
    README.md                         # operator bootstrap and recovery guide
```

### Task 1: Scaffold the isolated dashboard project and its durable settings

**Files:**
- Create: `work/vpn-dashboard/Dockerfile`
- Create: `work/vpn-dashboard/compose.yaml`
- Create: `work/vpn-dashboard/backend/pyproject.toml`
- Create: `work/vpn-dashboard/backend/app/settings.py`
- Create: `work/vpn-dashboard/backend/app/main.py`
- Create: `work/vpn-dashboard/deploy/dashboard.env.example`
- Test: `work/vpn-dashboard/backend/tests/test_settings.py`

**Interfaces:**
- Produces `Settings.from_env() -> Settings` with `mihomo_url`, `wgeasy_url`, `direct_rules_path`, `geodata_dir`, and `database_path`.
- Produces a health endpoint `GET /api/healthz -> {"status":"ok"}`.

- [ ] **Step 1: Write failing setting-validation tests**

```python
def test_rejects_a_non_lan_dashboard_bind() -> None:
    with pytest.raises(ValidationError):
        Settings(dashboard_bind="0.0.0.0:8080", **valid_values())

def test_accepts_the_nas_lan_bind() -> None:
    assert Settings(dashboard_bind="192.168.2.103:8088", **valid_values()).dashboard_bind.endswith(":8088")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_settings.py -q`

Expected: `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 3: Implement typed settings and the container skeleton**

```python
class Settings(BaseSettings):
    dashboard_bind: str = "192.168.2.103:8088"
    mihomo_url: AnyHttpUrl = "http://vpn-wireguard:9091"
    wgeasy_url: AnyHttpUrl = "http://vpn-wireguard:51821"
    database_path: Path = Path("/data/dashboard.sqlite3")
    direct_rules_path: Path = Path("/gateway/rules/direct.txt")
    geodata_dir: Path = Path("/gateway/geodata")
```

Create Compose network attachment:

```yaml
services:
  dashboard:
    build: .
    container_name: vpn-dashboard
    restart: unless-stopped
    ports: ["192.168.2.103:8088:8080"]
    networks: [vpn-gateway_default]
networks:
  vpn-gateway_default:
    external: true
```

- [ ] **Step 4: Run settings and health tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_settings.py -q`

Expected: `2 passed`.

### Task 2: Add persistence and historical delta accounting

**Files:**
- Create: `work/vpn-dashboard/backend/app/db.py`
- Create: `work/vpn-dashboard/backend/app/models.py`
- Create: `work/vpn-dashboard/backend/tests/test_collectors.py`

**Interfaces:**
- Produces `record_peer_snapshot(snapshot: PeerSnapshot, observed_at: datetime) -> None`.
- Produces `monthly_usage(period: YearMonth) -> list[UsageBucket]`.
- Stores `peer_snapshots`, `traffic_hourly`, `traffic_monthly`, `probe_events`, `route_events`, `audit_events`, and `geo_updates`.

- [ ] **Step 1: Write rollover tests**

```python
def test_counter_reset_never_creates_negative_usage(session):
    record_peer_snapshot(session, peer("desktop", rx=100, tx=40), at("2026-07-01T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=20, tx=10), at("2026-07-01T01:00:00Z"))
    assert monthly_usage(session, "2026-07")[0].received_bytes == 20
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_collectors.py::test_counter_reset_never_creates_negative_usage -q`

Expected: `ImportError` because the persistence layer is absent.

- [ ] **Step 3: Implement transactional snapshots**

Use integer byte counters, UTC timestamps, and `delta = current if current < previous else current - previous`; aggregate deltas to an hourly bucket inside one SQLite transaction. Keep raw snapshots for 90 days and perform downsampling in a daily retention job.

- [ ] **Step 4: Run persistence tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_collectors.py -q`

Expected: all snapshot, month/year aggregation, and retention tests pass.

### Task 3: Implement the read-only Mihomo adapter and service probes

**Files:**
- Create: `work/vpn-dashboard/backend/app/mihomo.py`
- Create: `work/vpn-dashboard/backend/app/services.py`
- Create: `work/vpn-dashboard/backend/tests/test_mihomo.py`

**Interfaces:**
- Produces `MihomoClient.version()`, `groups()`, `rules()`, `connections()`, `traffic()`, `proxy_delay(name, url)` and `geo_upgrade()`.
- Produces `ServiceProbe.check_all() -> list[ServiceStatus]`.

- [ ] **Step 1: Write protocol tests with mocked controller responses**

```python
async def test_connections_keep_matched_rule_and_chain(httpx_mock):
    httpx_mock.add_response(json={"connections":[{"id":"1", "chains":["VPS-FALLBACK","WG-IMP"], "rule":"MATCH"}]})
    item = (await client.connections())[0]
    assert item.chain[-1] == "WG-IMP"
    assert item.rule == "MATCH"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_mihomo.py -q`

Expected: `ModuleNotFoundError: app.mihomo`.

- [ ] **Step 3: Implement authenticated controller calls**

Every request adds `Authorization: Bearer <MIHOMO_API_SECRET>`. Use documented endpoints: `/version`, `/group`, `/rules`, `/providers/rules`, `/connections`, `/traffic`, `/proxies/{name}/delay`, `/upgrade/geo`, and `PUT /configs?force=true`. Treat expected controller `401` without a secret as an unhealthy integration, never as an anonymous success.

- [ ] **Step 4: Implement no-Docker-socket health probes**

Probe wg-easy HTTP, Mihomo `/version`, MetaCubeXD HTTP, and Kuma HTTP. A `200`, configured redirect, or authenticated controller response is healthy only for its expected endpoint; record reason and latency.

- [ ] **Step 5: Run adapter tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_mihomo.py -q`

Expected: all mocked controller and health-status tests pass.

### Task 4: Add the guarded wg-easy v15.2.2 profile adapter

**Files:**
- Create: `work/vpn-dashboard/backend/app/wgeasy.py`
- Create: `work/vpn-dashboard/backend/tests/test_wgeasy.py`
- Modify: `work/vpn-dashboard/backend/app/models.py`

**Interfaces:**
- Produces `WgEasyAdapter.verify_contract() -> ContractStatus`.
- Produces `list_clients()`, `create_client(name)`, `disable_client(client_id)`, `delete_client(client_id)`, `config(client_id)`, and `qrcode(client_id)`.
- Produces `ProfileMutationDisabled` when the version-specific contract fails.

- [ ] **Step 1: Write a contract guard test**

```python
async def test_mutation_is_blocked_if_v15_contract_shape_changes(httpx_mock):
    httpx_mock.add_response(url="http://vpn-wireguard:51821/api/client", json={"unexpected": True})
    adapter = WgEasyAdapter(settings)
    assert not (await adapter.verify_contract()).ready
    with pytest.raises(ProfileMutationDisabled):
        await adapter.create_client("laptop")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_wgeasy.py -q`

Expected: `ModuleNotFoundError: app.wgeasy`.

- [ ] **Step 3: Implement the isolated adapter**

Use Basic Authentication only inside the backend, with credentials supplied by dashboard setup and stored encrypted at rest. During boot, query the deployed v15.2.2 client-list API and validate the exact required fields: stable client ID, name, enabled flag, address, latest handshake, and transfer counters. Do not expose raw wg-easy responses to the browser. Map all vendor payloads to `WireGuardClient` schemas.

- [ ] **Step 4: Implement profile lifecycle tests**

Mock a create response, generated configuration download, QR payload, disable response, and deletion response. Assert audit records contain profile ID and name but not configuration text or keys.

- [ ] **Step 5: Run adapter tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_wgeasy.py -q`

Expected: contract mismatch, success lifecycle, and secret-redaction cases pass.

### Task 5: Add collectors, scheduled probes, rules revisions, and update audit

**Files:**
- Create: `work/vpn-dashboard/backend/app/collectors.py`
- Create: `work/vpn-dashboard/backend/app/rules.py`
- Create: `work/vpn-dashboard/backend/tests/test_rules.py`

**Interfaces:**
- Produces `Collector.run_minute()`, `Collector.run_probe_cycle()`, `Collector.run_daily()`.
- Produces `RuleService.preview_direct_rules(text) -> DirectRulesPreview` and `apply_direct_rules(text, actor) -> RuleRevision`.

- [ ] **Step 1: Write failing rule validation tests**

```python
def test_rejects_an_invalid_direct_rule(tmp_path):
    with pytest.raises(DirectRuleValidationError):
        service.preview_direct_rules("NOT-A-RULE bad domain")

def test_apply_creates_revision_before_mihomo_reload(tmp_path, mihomo):
    revision = service.apply_direct_rules("DOMAIN-SUFFIX,example.org,DIRECT\n", actor="admin")
    assert revision.number == 1
    mihomo.reload.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_rules.py -q`

Expected: `ModuleNotFoundError: app.rules`.

- [ ] **Step 3: Implement event collectors**

Minute job records peer counters, handshakes, service status and active fallback group. Five-minute job requests delay for `WG-IMP` and `HY2-NL` against Cloudflare `generate_204`, GitHub API and Google `generate_204`, then records endpoint, status, latency, selected outbound and error text. When active outgoing proxy changes, insert a route event with the preceding and new proxy.

- [ ] **Step 4: Implement safe local rules workflow**

Parse only `DOMAIN`, `DOMAIN-SUFFIX`, `DOMAIN-KEYWORD`, `IP-CIDR`, `GEOIP`, `GEOSITE`, and terminal `DIRECT` forms. Write a timestamped revision under `/data/rule-revisions/`, atomically replace the mounted `direct.txt`, call `PUT /configs?force=true`, and restore the previous file if reload fails.

- [ ] **Step 5: Implement daily GEO checks**

Read GeoIP/GeoSite file names, mtimes and SHA-256 from the read-only mount. The manual button calls `POST /upgrade/geo`; record pre/post metadata and the controller result. Do not write downloaded Geo files directly.

- [ ] **Step 6: Run collector and rules tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_collectors.py tests/test_rules.py -q`

Expected: traffic, fallback event, revision rollback and GEO audit tests pass.

### Task 6: Expose a protected, audit-friendly FastAPI surface

**Files:**
- Create: `work/vpn-dashboard/backend/app/auth.py`
- Create: `work/vpn-dashboard/backend/app/schemas.py`
- Create: `work/vpn-dashboard/backend/app/routes.py`
- Create: `work/vpn-dashboard/backend/tests/test_auth.py`
- Create: `work/vpn-dashboard/backend/tests/test_routes.py`

**Interfaces:**
- Produces bootstrap, login, logout and `require_admin` dependencies.
- Produces `/api/overview`, `/api/routes`, `/api/clients`, `/api/rules`, `/api/updates`, `/api/journal`.
- Produces POST mutation endpoints with CSRF protection and audit logging.

- [ ] **Step 1: Write security-first route tests**

```python
def test_anonymous_client_cannot_download_config(client):
    assert client.get("/api/clients/42/config").status_code == 401

def test_overview_never_contains_secret(client, admin_session):
    payload = client.get("/api/overview").text
    assert "MIHOMO_API_SECRET" not in payload
    assert "private_key" not in payload.lower()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_auth.py tests/test_routes.py -q`

Expected: missing application routes.

- [ ] **Step 3: Implement bootstrap and authenticated sessions**

First visit may create one owner password using Argon2id. Use an HttpOnly, Secure, SameSite=Strict session cookie, CSRF token on mutations, and a startup state that disallows unauthenticated dashboard access after bootstrap. Enforce LAN binding in Compose rather than trusting a forwarded header.

- [ ] **Step 4: Implement typed read and mutation routes**

Use response schemas that expose client config only as a `text/plain` attachment from an authenticated download route; never put configuration text into JSON, history, telemetry or audit records. All profile/rule/GEO mutations add an audit event.

- [ ] **Step 5: Run API tests**

Run: `cd work/vpn-dashboard/backend && pytest tests/test_auth.py tests/test_routes.py -q`

Expected: authentication, secret-redaction, CSRF and audit coverage pass.

### Task 7: Build the React interface with two distinct operational pages

**Files:**
- Create: all `work/vpn-dashboard/frontend/src/**` files listed in File Structure
- Test: `work/vpn-dashboard/frontend/src/pages/*.test.tsx`

**Interfaces:**
- Consumes REST types from `GET /api/overview` and `GET /api/routes`.
- Produces visible pages `/overview`, `/routes`, `/clients`, `/rules`, `/updates`, `/journal`.

- [ ] **Step 1: Write navigation and fallback-language tests**

```tsx
it('labels HY2 as reserve instead of a chained route', async () => {
  render(<RoutesPage data={routeFixture({ active: 'WG-IMP', reserve: 'HY2-NL' })} />)
  expect(screen.getByText('Основной: WG-IMP')).toBeVisible()
  expect(screen.getByText('Резервный: HY2-NL')).toBeVisible()
  expect(screen.queryByText('WG-IMP → HY2-NL')).not.toBeInTheDocument()
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd work/vpn-dashboard/frontend && npm test -- --run`

Expected: missing page components.

- [ ] **Step 3: Implement the application shell and Overview page**

Use the first approved visual direction: left rail; a shallow service table; separate cards/rows for `Основной: WG-IMP` and `Резервный: HY2-NL`; active client table; monthly/year traffic; latest events; GeoIP/GeoSite status. Do not render a serial route diagram on this page.

- [ ] **Step 4: Implement the Routes page**

Use the third approved visual direction as a separate page: one central branch from Mihomo to a solid primary branch `WG-IMP` and dotted reserve branch `HY2-NL`; inspector with three most recent probes, selected exit, last switch reason and current connections. Use responsive SVG only for connector lines; all status labels remain semantic HTML.

- [ ] **Step 5: Implement management pages**

Clients page creates profiles, offers QR/config downloads and shows month/year usage. Rules page previews/applies custom DIRECT changes and shows active Mihomo rule/provider rows. Updates page requests GEO upgrade and displays audit. Journal supports time range, event type, outbound and endpoint filters.

- [ ] **Step 6: Run frontend tests and production build**

Run: `cd work/vpn-dashboard/frontend && npm test -- --run && npm run build`

Expected: all tests pass and `dist/` is produced.

#### Task 7 QA fix evidence (2026-07-13)

- Regression coverage added in `src/App.test.tsx`: successful `GET /api/auth/csrf` returns `AuthSession`, and `/routes` renders after the session query succeeds.
- TDD red state confirmed before the fix: `restoreSession()` resolved to `undefined`; TanStack Query rejected query key `['session', 0]`, and the protected route redirected to login.
- `restoreSession()` now returns the stored `AuthSession`; `index.html` has an explicit inline SVG data favicon, so no `/favicon.ico` request is needed.
- Verification: `npm test -- --run` — 3 files / 4 tests passed; `npm run build` — completed successfully and emitted `dist/`.

### Task 8: Deploy as a separate Synology project and execute end-to-end verification

**Files:**
- Modify: `work/vpn-dashboard/compose.yaml`
- Create: `work/vpn-dashboard/deploy/README.md`
- Modify: `/volume1/docker/vpn-dashboard/compose.yaml` by deployment upload only

**Interfaces:**
- Consumes the existing Docker network `vpn-gateway_default`, `vpn-wireguard`, `vpn-uptime-kuma`, and Mihomo controller.
- Produces the LAN-only dashboard at `http://192.168.2.103:8088`.

- [ ] **Step 1: Validate before deployment**

Run locally: `docker compose -f work/vpn-dashboard/compose.yaml config`.

Expected: exactly one `vpn-dashboard` service, one external network, and one LAN-bound host port.

- [ ] **Step 2: Create NAS secret and data paths with restrictive permissions**

Create `/volume1/docker/vpn-dashboard/secrets/dashboard.env` and `/volume1/docker/vpn-dashboard/data`; set secret file to `0600`. Populate `MIHOMO_API_SECRET` and dashboard encryption material through DSM/SSH without printing values. Leave wg-easy integration empty until its credentials are entered in the dashboard bootstrap flow.

- [ ] **Step 3: Create the separate Container Manager project**

Upload the project to `/volume1/docker/vpn-dashboard`, register it as `vpn-dashboard`, build it, and start it. Verify `vpn-gateway` was not stopped, recreated or altered.

- [ ] **Step 4: Perform functional verification**

1. From NAS/LAN, `curl -I http://192.168.2.103:8088/api/healthz` returns `200`.
2. From WAN, the same dashboard port is unreachable.
3. Dashboard bootstrap creates its owner account; anonymous API call returns `401`.
4. Overview reports four live services, `WG-IMP` primary and `HY2-NL` reserve.
5. Routes page shows live Mihomo connections and the last three checks with endpoint, time and RTT.
6. Connect a WireGuard client, then confirm its handshake and increased monthly delta appear in dashboard.
7. Disable the primary exit in the controlled test, confirm a `WG-IMP → HY2-NL` *switch event* (not a chain) and restore it.
8. Add a harmless temporary DIRECT rule, validate/reload, confirm audit and then restore the prior revision.
9. Trigger GEO update, confirm an audit record with before/after metadata.

- [ ] **Step 5: Verify recovery**

Restart only `vpn-dashboard`; confirm it reconnects, retains owner bootstrap state, snapshots and journal data. Restore its SQLite backup into a scratch copy and run `sqlite3 scratch.sqlite3 'pragma integrity_check;'`; expected result is `ok`.
