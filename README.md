# VPN Gateway Dashboard

Панель управления VPN-шлюзом Synology DS923+. Она обслуживает WireGuard-клиентов, показывает состояние Mihomo и резервных выходов, управляет профилями wg-easy, правилами маршрутизации и локальным аудитом.

## Что входит

- `backend/` — FastAPI, сбор состояния Mihomo/wg-easy, аудит, GeoData и API панели.
- `frontend/` — React/Vite-интерфейс: клиенты, маршруты, правила, обновления и журнал.
- `compose.yaml` — отдельный Docker Compose-проект `vpn-dashboard`.
- `deploy/dashboard.env.example` — безопасный шаблон переменных окружения.
- `docs/` — утверждённые архитектурные спецификации и планы.

## Архитектура

WireGuard-клиенты подключаются к NAS, а Mihomo выбирает исходящий маршрут в порядке `WG-IMP → HY2-NL`. Панель не хранит конфигурации WireGuard в браузере и не выводит секреты в API или журнал.

Основные возможности:

- управление профилями wg-easy: создание, переименование, включение, отключение и отзыв;
- QR-код и `.conf` только по явному запросу;
- мониторинг двух независимых выходов, ручной запуск проверок и безопасные пользовательские HTTPS-цели;
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
