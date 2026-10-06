# VPN Gateway Dashboard

Панель управления VPN-шлюзом Synology DS923+. Она обслуживает WireGuard-клиентов, показывает состояние Mihomo и резервных выходов, управляет профилями wg-easy, правилами маршрутизации и локальным аудитом.

## Что входит

- `backend/` — FastAPI, сбор состояния Mihomo/wg-easy, аудит, GeoData и API панели.
- `frontend/` — React/Vite-интерфейс: клиенты, маршруты, правила, обновления и журнал.
- `compose.yaml` — отдельный Docker Compose-проект `vpn-dashboard`.
- `deploy/dashboard.env.example` — безопасный шаблон переменных окружения.
- `docs/` — утверждённые архитектурные спецификации и планы.

## Архитектура

WireGuard-клиенты подключаются к NAS, а Mihomo выбирает основной или резервный исходящий маршрут. `WG-IMP` и `HY2-USA` — совместимые технические ID, а не достоверные названия протокола и страны: последнее подтверждённое состояние от 27.09.2026 — VLESS-NL и HY2-DE. См. [историю и ограничения проверки](docs/operations.md). Ключи клиентов сохраняются отдельно от исходного кода; QR/конфигурация передаются браузеру только по запросу.

Основные возможности:

- управление профилями wg-easy: создание, переименование, включение, отключение и отзыв;
- QR-код и `.conf` только по явному запросу;
- мониторинг двух независимых выходов, ручной запуск проверок и безопасные пользовательские HTTPS-цели;
- лёгкий график скорости канала Mihomo за 5 минут, 30 минут или 6 часов: один сбор в минуту, чтение графика — только из SQLite;
- правила GeoSite/GeoIP с выбором `DIRECT`, fallback-группы или конкретного выхода;
- отображение фактического времени GeoData, обновлений и аудита действий администраторов.

## Локальная разработка

Требуются Python 3.12+ и Node.js 22+.

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m pytest

cd ../frontend
npm ci
npm test -- --run
npm run build
```

## Запуск в Docker

1. Скопируйте `deploy/dashboard.env.example` в `deploy/dashboard.env`.
2. Заполните секреты только в `deploy/dashboard.env`; этот файл намеренно исключён из Git.
3. Убедитесь, что внешняя сеть `vpn-gateway_default` уже существует.
4. Соберите и запустите панель:

```powershell
docker compose up -d --build dashboard
```

Для Synology проект развёрнут в `/volume1/docker/vpn-dashboard`. Сборка меняет только контейнер `vpn-dashboard`; существующие VPN-контейнеры не затрагиваются.

## Healthcheck VPN-контейнеров

Источник конфигурации шлюза хранится в `deploy/gateway/compose.yaml`. Скрипт ниже добавляет healthcheck без изменения WireGuard, его клиентов или ключей:

```powershell
D:\Projects\vpn-gateway-dashboard\deploy\apply-gateway-healthchecks.ps1
```

Перед заменой Compose-файла он сверяет хеш текущей конфигурации NAS, проверяет живой контроллер Mihomo и HTTP-ответ MetaCubeXD, сохраняет резервную копию и при неудаче возвращает прежний Compose. На время применения перезапускаются только `vpn-mihomo` и `vpn-metacubexd`; WireGuard продолжает работать. После этого DSM покажет `healthy` или `unhealthy` у обоих контейнеров. Проверка Mihomo контролирует процесс и локальный TCP-порт API `9091`, а MetaCubeXD — реальный ответ на HTTP-запрос к собственной странице.

## Замена резервного Hysteria2

```powershell
D:\Projects\vpn-gateway-dashboard\deploy\replace-hy2-profile.ps1
```

Это прежний миграционный скрипт `HY2-NL → HY2-USA`, не команда планового обновления уже заменённого резерва. Не запускайте его для повторной настройки Германии. Текущее состояние и процедура замены описаны в [operations.md](docs/operations.md); секреты нельзя передавать в аргументах, коммитах или журналах.

## Российские маркетплейсы и доставка

Для Ozon, Wildberries, Авито и Яндекса сначала предпочтительны точные GeoSite-политики `DIRECT` из раздела «04 / Правила». Они обновляются вместе с GeoSite. Самокат, Купер и Delivery Club не имеют столь же узкого общего GeoSite-тега, поэтому для них есть консервативный набор доменных DIRECT-правил:

```powershell
D:\Projects\vpn-gateway-dashboard\deploy\apply-russian-commerce-delivery-direct.ps1
```

Команда импортирует только отсутствующие правила, перед изменением создаёт зашифрованный backup `direct.txt`, применяет изменения одним reload Mihomo и не меняет WireGuard-клиентов, ключи, профили выходов или fallback. После завершения откройте по одному проблемному приложению на телефоне и проверьте фактическую цепочку без изменений конфигурации:

```powershell
D:\Projects\vpn-gateway-dashboard\deploy\diagnose-russian-services-routing.ps1 -WatchSeconds 60
```

В строках `WATCH_*` ожидается `CHAIN=DIRECT`. Если для приложения отображается `HOST=unknown`, не добавляйте IP-адрес вслепую: сохраните вывод диагностики — он покажет, требуется ли уточнить DNS, SNI или отдельный домен API.

## Безопасность

- Не добавляйте в Git `deploy/dashboard.env`, базу SQLite, архивы конфигураций или WireGuard-профили.
- Для пользовательских проверок разрешены только публичные HTTPS-хосты на порту `443`; IP-адреса, учётные данные в URL, параметры и локальные сети отклоняются.
- Перед публикацией выполните тесты из корня проекта:

```powershell
python -m pytest backend/tests -q
python -m unittest discover -s deploy/tests
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

## Git

Основной репозиторий — [Mgkgr/vpn_synology](https://github.com/Mgkgr/vpn_synology), ветка `main`. Рабочий checkout — `D:\Projects\vpn-gateway-dashboard`; `origin` указывает на этот репозиторий. Репозиторий публичный: публикуются только проверенные исходники и документация.

Репозиторий уже содержит историю проекта. В Git храним код, тесты, документацию и безопасные шаблоны — **не** базы клиентов, ключи, токены, GeoData-файлы или архивы. Их исключения заданы в `.gitignore`; уже отслеживаемые файлы одним `.gitignore` не скрываются.

Перед коммитом и перед push выполните локальную проверку (Git + Python 3.12+, проверенная версия Gitleaks — 8.30.1):

```powershell
python deploy/check-git-publication.py --gitleaks "C:\Tools\gitleaks.exe"
if ($LASTEXITCODE -ne 0) { throw 'Publication check failed' }
git status --short
git diff --stat
```

Укажите свой путь к Gitleaks либо установите `GITLEAKS_PATH`. Проверка охватывает всю историю, индекс и текущие неигнорируемые файлы, не меняет индекс и ничего не публикует. Добавляйте только просмотренные файлы по явным путям; проверка не заменяет просмотр diff.

По решению владельца от 04.10.2026 внешнюю резервную копию не настраиваем. **Git не заменяет локальную зашифрованную копию NAS**: только она сохраняет базу wg-easy, ключ сервера и профили, необходимые для восстановления без перенастройки клиентов. При утрате самого NAS/диска эта локальная копия тоже может быть потеряна.

Подробнее: [публикация в Git](docs/git-publication.md), [незавершённые работы](docs/diagnostics/2026-10-04-outstanding-work.md), [подключение Telegram](docs/telegram-monitoring.md).
