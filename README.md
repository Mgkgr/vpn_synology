# VPN Gateway Dashboard

Панель управления VPN-шлюзом Synology DS923+. Она обслуживает WireGuard-клиентов, показывает состояние Mihomo и резервных выходов, управляет профилями wg-easy, правилами маршрутизации и локальным аудитом.

## Что входит

- `backend/` — FastAPI, сбор состояния Mihomo/wg-easy, аудит, GeoData и API панели.
- `frontend/` — React/Vite-интерфейс: клиенты, маршруты, правила, обновления и журнал.
- `compose.yaml` — отдельный Docker Compose-проект `vpn-dashboard`.
- `deploy/dashboard.env.example` — безопасный шаблон переменных окружения.
- `docs/` — утверждённые архитектурные спецификации и планы.

## Архитектура

WireGuard-клиенты подключаются к NAS, а Mihomo выбирает исходящий маршрут в порядке `WG-IMP → HY2-USA`. Панель не хранит конфигурации WireGuard в браузере и не выводит секреты в API или журнал.

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

Команда атомарно переводит действующий резерв с `HY2-NL` на `HY2-USA`: меняет Hysteria2-блок, ссылку fallback и provider правил, сохраняя правила панели и основной `WG-IMP`. Два секрета запрашиваются в маскированном виде. Перед успехом требуется валидная конфигурация, hot-reload и положительная отдельная проверка задержки через новый HY2; при ошибке автоматически возвращается предыдущая версия.

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
- Перед публикацией проверьте статус:

```powershell
python -m pytest -q
cd ../frontend; npm test -- --run; npm run build
```

## Git

Репозиторий инициализирован локально. Перед первым коммитом:

```powershell
git status
git add .
git commit -m "Initial VPN gateway dashboard"
```
