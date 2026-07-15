# P2P and Streaming Routing Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Расширить защищённый каталог GeoSite на странице «Правила» отдельными группами P2P/трекеров, видео и стриминга, игр/загрузок и специальных категорий.

**Architecture:** Backend остаётся единственным allowlist-источником поддерживаемых MetaCubeX-тегов и продолжает валидировать каждую пару `kind + category`. Frontend получает этот конечный каталог через существующий `/api/rules`, раскладывает GeoSite-теги по понятным тематическим блокам и использует существующее создание managed-rule. Никакие правила, провайдеры и маршруты не создаются автоматически.

**Tech Stack:** Python 3.12, FastAPI, pytest, React 19, TypeScript, TanStack Query, Vitest, Testing Library, MetaCubeX `meta-rules-dat`.

## Global Constraints

- Использовать только проверенные на 15 июля 2026 года теги из ветки `meta` официального `MetaCubeX/meta-rules-dat`: не принимать от UI произвольный GeoSite.
- Не менять существующие правила, `direct.txt`, fallback `WG-IMP → HY2-NL`, GeoData scheduler, контейнеры, порты или DSM firewall.
- GeoSite классифицирует техническую тематику доменов, а не правовой статус. Не называть наборы «пиратскими сервисами» и не обещать перехват всего P2P-трафика.
- Для широких наборов выводить короткое предупреждение из backend-каталога; точные сервисы сохранять отдельными элементами.
- Не выводить секреты, адреса выходов или содержимое конфигураций Mihomo в API, тестах или интерфейсе.
- Следовать TDD: сначала добавить и запустить падающий тест, затем минимальную реализацию и повторно запустить тест.
- Перед live-деплоем включить отдельный mount GeoData/правил из коммита `c3859bf`; пока он не применён, операции с правилами на NAS останутся недоступны из-за прав доступа к родительскому mount.

---

## File structure

- `backend/app/policy_rules.py` — finite allowlist MetaCubeX-тегов, названия и безопасные пояснения.
- `backend/tests/test_routes.py` — контракт `/api/rules` и валидация созданных managed-rules.
- `frontend/src/pages/RulesPage.tsx` — тематическая группировка карточек GeoSite без изменения их действия.
- `frontend/src/pages/RulesPage.test.tsx` — пользовательский сценарий выбора P2P-категории и видимость тематических блоков.
- `docs/superpowers/specs/2026-07-15-routing-catalog-design.md` — утверждённые границы и смысл категорий.

## Task 1: Backend allowlist для P2P, стриминга, игр и специальных наборов

**Files:**
- Modify: `backend/app/policy_rules.py`
- Modify: `backend/tests/test_routes.py`

**Interfaces:**
- `GET /api/rules` продолжает возвращать `policy_catalog: list[ManagedRuleCategoryResponse]`; новые записи имеют только `kind`, `category`, `label`, `description`.
- `POST /api/policy-rules` и `PUT /api/policy-rules/{id}` принимают новые категории исключительно потому, что они присутствуют в `POLICY_CATEGORIES`.

- [ ] **Step 1: Написать падающие API-тесты контракта и валидации.**

  Добавить в `test_rules_exposes_the_verified_policy_catalog` (или соседний специализированный тест) точные пары:

  ```python
  required = {
      ("GEOSITE", "category-public-tracker"),
      ("GEOSITE", "category-pt"),
      ("GEOSITE", "tracker"),
      ("GEOSITE", "category-entertainment"),
      ("GEOSITE", "category-media"),
      ("GEOSITE", "disney"),
      ("GEOSITE", "hbo"),
      ("GEOSITE", "primevideo"),
      ("GEOSITE", "twitch"),
      ("GEOSITE", "dazn"),
      ("GEOSITE", "bilibili"),
      ("GEOSITE", "biliintl"),
      ("GEOSITE", "anime"),
      ("GEOSITE", "ehentai"),
      ("GEOSITE", "category-porn"),
      ("GEOSITE", "category-games"),
      ("GEOSITE", "category-game-platforms-download"),
      ("GEOSITE", "category-android-app-download"),
      ("GEOSITE", "steam"),
      ("GEOSITE", "category-ads-all"),
      ("GEOSITE", "speedtest"),
  }
  assert required <= {(item["kind"], item["category"]) for item in payload["policy_catalog"]}
  ```

  Добавить запрос создания `category-public-tracker` с `VPS-FALLBACK`; подтвердить успешный ответ и сохранённую пару категории/маршрута. Отрицательный тест для произвольного `category-p2p` сохранить: этот несуществующий тег не должен стать допустимым.

- [ ] **Step 2: Запустить focused backend-тест и подтвердить red.**

  Run: `Push-Location backend; python -m pytest tests/test_routes.py -k "catalogue or public_tracker" -v; Pop-Location`

  Expected: FAIL, поскольку новых записей нет в `POLICY_CATEGORIES`.

- [ ] **Step 3: Минимально расширить finite catalog.**

  Добавить `PolicyCategory` в тематическом порядке:

  1. P2P: `category-public-tracker`, `category-pt`, `tracker`.
  2. Видео: `category-entertainment`, `category-media`, `disney`, `hbo`, `primevideo`, `twitch`, `dazn`, `bilibili`, `biliintl`.
  3. Игры: `category-games`, `category-game-platforms-download`, `category-android-app-download`, `steam`.
  4. Специальные: `anime`, `ehentai`, `category-porn`, `category-ads-all`, `speedtest`.

  Для трекеров указать, что сопоставление идёт по домену и не охватывает весь протокол BitTorrent. Для `category-entertainment`, `category-media`, `anime`, `category-porn`, `category-ads-all` и `speedtest` передать `description` с ограничением широкого воздействия. Для точных сервисов использовать краткое нейтральное описание. Не менять `_CATEGORIES`, `POLICY_ACTIONS`, генерацию provider-файлов или откат reload.

- [ ] **Step 4: Запустить focused backend-тест и подтвердить green.**

  Run: `Push-Location backend; python -m pytest tests/test_routes.py -k "catalogue or public_tracker" -v; Pop-Location`

  Expected: PASS. `category-public-tracker` создаётся, `category-p2p` остаётся отклонённым.

- [ ] **Step 5: Зафиксировать атомарный backend-этап.**

  Run: `git add backend/app/policy_rules.py backend/tests/test_routes.py && git commit -m "feat: add verified p2p and streaming geosite catalog"`

  Expected: один коммит только с allowlist и его тестами.

## Task 2: Тематические карточки каталога на странице «Правила»

**Files:**
- Modify: `frontend/src/pages/RulesPage.tsx`
- Modify: `frontend/src/pages/RulesPage.test.tsx`

**Interfaces:**
- `CategoryPicker` остаётся controlled-компонентом: клик по карточке вызывает текущий `onPick(item)` и не создаёт правила.
- Каждая GeoSite-категория рендерится максимум в одном блоке; GeoIP остаётся в текущем блоке `Сети и страны GeoIP`.

- [ ] **Step 1: Написать падающий UI-тест.**

  В mock `api.rules()` включить по одному элементу из каждого нового блока, включая `category-public-tracker`, `category-entertainment`, `category-games`, `speedtest`, и обычный `openai` плюс один `GEOIP`. Проверить заголовки:

  ```tsx
  expect(await screen.findByRole('heading', { name: 'P2P / торренты' })).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Видео и стриминг' })).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Игры и загрузки' })).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Инфраструктура и специальные категории' })).toBeVisible()
  ```

  Выбрать «Публичные торрент-трекеры», выбрать текущей карточкой `VPS-FALLBACK`, нажать «Добавить правило» и подтвердить точный payload вызова `api.createPolicy`:

  ```tsx
  { kind: 'GEOSITE', category: 'category-public-tracker', action: 'VPS-FALLBACK', enabled: true }
  ```

  Также проверить видимость предупреждения о доменном характере tracker-набора и отсутствие какого-либо вызова создания правила до нажатия кнопки.

- [ ] **Step 2: Запустить focused frontend-тест и подтвердить red.**

  Run: `npm --prefix frontend test -- --run src/pages/RulesPage.test.tsx`

  Expected: FAIL, поскольку все новые GeoSite-теги пока попадут в единый блок «Популярные сайты и сервисы».

- [ ] **Step 3: Реализовать стабильную группировку.**

  В `RulesPage.tsx` определить `Set` для Russian, P2P, streaming, games/downloads и special GeoSite-категорий. Создать блоки в последовательности:

  1. «Российские сервисы» (если есть),
  2. «P2P / торренты» (если есть),
  3. «Видео и стриминг» (если есть),
  4. «Игры и загрузки» (если есть),
  5. «Инфраструктура и специальные категории» (если есть),
  6. «Популярные сайты и сервисы» — только нераспределённый GeoSite,
  7. «Сети и страны GeoIP».

  Фильтровать каждый блок из исходного `catalog`, чтобы порядок в backend оставался источником порядка карточек. Перед вычислением `popular` исключить объединение всех известных тематических множеств; это исключит дублирование. Сохранить `description` карточек как единственный источник предупреждений. Не менять форму расширенного выбора и не добавлять browser alert/confirm.

- [ ] **Step 4: Запустить focused frontend-тест и подтвердить green.**

  Run: `npm --prefix frontend test -- --run src/pages/RulesPage.test.tsx`

  Expected: PASS; карточка P2P выбирается, а правило создаётся только явной кнопкой пользователя.

- [ ] **Step 5: Зафиксировать атомарный frontend-этап.**

  Run: `git add frontend/src/pages/RulesPage.tsx frontend/src/pages/RulesPage.test.tsx && git commit -m "feat: group p2p and streaming routing categories"`

## Task 3: Полная регрессия и контролируемый live-деплой

**Files:**
- No source changes expected.

- [ ] **Step 1: Выполнить полный локальный контроль.**

  Run: `Push-Location backend; python -m pytest -v; Pop-Location`

  Run: `npm --prefix frontend test -- --run`

  Run: `npm --prefix frontend run build`

  Expected: все тесты и production-build проходят; отсутствие предупреждений TypeScript/Vite, влияющих на результат.

- [ ] **Step 2: Проверить diff и план деплоя.**

  Run: `git status --short && git log --oneline -3 && git diff HEAD~2..HEAD --check`

  Expected: только два новых тематических коммита, отсутствуют trailing whitespace и незапланированные файлы.

- [ ] **Step 3: Применить сначала исправление mount прав, затем версию каталога.**

  User runs: `D:\Projects\vpn-gateway-dashboard\deploy\deploy-hardened.ps1`

  Expected: контейнер `vpn-dashboard` имеет состояние `running`; deployment включает уже подготовленный isolated mount `/geodata` и `/gateway/rules`, поэтому dashboard может читать `direct.txt` и менять managed rules.

- [ ] **Step 4: Проверить live API без раскрытия секретов.**

  Run (через существующий restricted helper): `sudo -n /usr/local/sbin/vpn-dashboard-diagnose-api 2>&1; sudo -n /usr/local/sbin/vpn-gateway-rules-diagnostic 2>&1`

  Expected: `/api/rules` отвечает `HTTP_200`, `DIRECT=success`, и in-container каталог показывает новые пары. При ошибке доступа сначала остановиться на диагностике mount/прав, не изменять правила вручную.

- [ ] **Step 5: Проверить в браузере один явно созданный тестовый маршрут.**

  В «Правилах» выбрать P2P или точный сервис, назначить нужный маршрут, создать правило и убедиться, что оно мгновенно появилось в таблице. Затем удалить тестовое правило и убедиться, что UI и Mihomo reload завершились успешно. Не создавать широкое правило `category-entertainment` без осознанного выбора пользователя.
