# TIGRWEBSITE: второй проход исправления безопасности

Дата локальной проверки: 10 сентября 2026. Исходный HEAD до правок:
`890398e86032779d2fdeececf6ef42af03afc01e` (`Security updates`), рабочее дерево было чистым.
Упомянутого отдельного файла `TIGRWEBSITE_AUDIT_890398e.md` в репозитории и каталоге
полученного задания не было; границы проверки взяты из предоставленного текста задания.

Проверка и исправления выполнены локально на временных синтетических БД и loopback-listener.
Реальные `.env`, `data`, заказы, прайсы и production-сервис не читались и не изменялись;
push/deploy/production scan не выполнялись.

## Результат по пунктам

| Пункт | Статус | Исправление и доказательство |
| --- | --- | --- |
| R2-01: медленное/зависшее тело запроса | Исправлено | `SecurityEnvelope` применяет idle и фиксированный total deadline: формы 30/180 s, импорт 90/1800 s. Таймаут и ранний disconnect до старта ответа дают один 408; лимиты 16 KiB/64 KiB/128 MiB и 413 сохранены. Multipart spool и межпроцессный upload lock освобождаются в shielded cleanup. Caddy 2.11.4 принимает явный `servers.timeouts.read_header 10s`. Тесты покрывают stalled body, byte drip, disconnect, валидную форму, медленную загрузку импорта, освобождение lock и четыре параллельных stall. Реальный временный Caddy 2.11.4 → Uvicorn health и slow-header probe прошёл. |
| R2-02: развёрнутый текст XLSX | Исправлено | Добавлены отдельные бюджеты: shared table 8 MiB, развёрнутые значения 32 MiB; каждая ссылка shared string, inline/rich text, обычное текстовое значение и имя листа учитываются в UTF-8 байтах до помещения значения в строки и до бизнес-нормализации. Бизнес-пределы: sheet/brand/category/color 512 B, model 1024 B, box 128 B, description 64 KiB, comment 8 KiB, badge 1024 B. Превышение — `ImportLimitError` до DB/файлов. Тесты используют реальные ZIP/XLSX с повторным shared string, inline, rich text, Unicode и несколькими листами при уменьшенном test budget; каталог и временные файлы остаются неизменными. Обычный и повторный импорт сохранены. |
| R2-03: длинная SQLite writer transaction | Исправлено | ZIP/XML parse, Pillow verify/full decode, hashing и запись временных изображений выполняются до `BEGIN IMMEDIATE`; повторного decode при публикации нет. Под writer lock остаются быстрый rename и атомарный upsert/import-run. Ошибка удаляет только новые неиспользуемые файлы после проверки ссылок; shared/старые файлы сохраняются. SQLite остаётся в WAL, busy wait сокращён до 3 s; контролируемый lock переводится в 503 + `Retry-After: 1`. `last_seen` обновляется не чаще раза в 60 s (и не реже четверти короткого idle window), а не на каждом запросе. Тест блокирует image preparation и одновременно успешно создаёт checkout; отдельные тесты подтверждают один decode, rollback cleanup, 503 и идемпотентный retry. |
| R2-04: CSV в памяти | Исправлено | Приватный export использует отдельный producer thread, открывает/читает/закрывает SQLite на нём, вызывает `fetchmany(500)` и держит bounded queue из двух chunks. Async generator останавливает producer и закрывает connection при отмене/закрытии клиента. UTF-8 BOM только в первом chunk, `;`, заголовки, formula escaping, auth и `no-store` сохранены; история не удаляется. Тест на 1203 строки принудительно использует batches по 37 строк и тестирует раннее закрытие consumer. |
| R2-05: CI false positives / Dependabot | Проверено, точечное изменение не требуется | Текущий redacted scanner не находит кандидатов в актуальном `shop/security.py`; оба указанных отпечатка не воспроизводятся. Полный worktree+history scan: 71 файл, 89 исторических blobs, 8 совпадений с уже рассмотренными примерами, 0 непроверенных кандидатов. `.secrets-allowlist.json` не расширялся. Новый синтетический AWS fixture детектируется и не находится в allowlist. Публичный GitHub API подтверждает failure run `34471133767`, шаг `Run Dependabot`; единственная annotation сообщает лишь общий updater error и требует write access для подробного лога. ZIP logs вернул 403. Поэтому `.github/dependabot.yml`, версии и hashes вслепую не менялись. |

## Выполненные проверки

| Команда | Результат |
| --- | --- |
| `.\.venv-312\Scripts\python.exe -m pytest -q` | **224 passed**, 2 upstream warnings, 35.13 s |
| `.\.venv-313\Scripts\python.exe -m pytest -q` | **224 passed**, 2 upstream warnings, 35.07 s |
| `.\.venv-312\Scripts\python.exe -m pip check` и Python 3.13 equivalent | `No broken requirements found.` |
| `.\.venv-312\Scripts\python.exe -m compileall -q app.py shop scripts tests` и Python 3.13 equivalent | PASS |
| `.\.venv-audit\Scripts\python.exe -m pip_audit --require-hashes --disable-pip -r requirements.lock ...` | `No known vulnerabilities found` |
| Та же команда с `requirements-dev.lock` | `No known vulnerabilities found` |
| `.\.venv-312\Scripts\python.exe -m bandit -q -r shop scripts app.py` | PASS, exit 0, нет findings; только сообщения о существующих точечных `nosec` |
| `.\.venv-312\Scripts\python.exe scripts\scan_secrets.py --history` | PASS: 71 worktree files, 89 history blobs, 8 reviewed examples, 0 unreviewed candidates |
| `$env:DOMAIN='shop.example.invalid'; docker compose --env-file .env.example -f docker-compose.yml config --no-env-resolution --quiet` | PASS, exit 0; реальный `.env` не читался |
| Та же команда с `docker-compose.prod.yml` | PASS, exit 0 |
| `$env:DOMAIN='shop.example.invalid'; .\.security-tools\caddy-release\caddy.exe validate --config Caddyfile.example --adapter caddyfile` | PASS: `Valid configuration`, локальный Caddy v2.11.4 |
| `.\.venv-312\Scripts\python.exe scripts\check_caddy.py --binary .security-tools\caddy-release\caddy.exe` | 8 checks PASS: реальные Caddy 413/502 и защитные headers |
| `.\.venv-312\Scripts\python.exe scripts\check_caddy_uvicorn.py --binary .security-tools\caddy-release\caddy.exe` | PASS: временный loopback Caddy 2.11.4 → Uvicorn, health 200, неполный заголовок завершён 408 или закрытием соединения |
| `docker build -t tigr-security-r2-check .` | **NOT RUN**: после повтора вне sandbox Docker Desktop Linux daemon отсутствовал (`dockerDesktopLinuxEngine` pipe not found); сборка не началась |

Два pytest warning принадлежат закреплённым Starlette/FastAPI test helpers (переход на httpx2 и
устаревший AnyIO alias); они не скрывались. Compose сообщил предупреждение о недоступном sandbox
Docker config, но оба `config --quiet` завершились с exit 0.

## Совместимость и оставшиеся ограничения

- Публичная схема каталога и заказов не менялась; существующие product/order/public token и история
  сохраняются. Checkout idempotency, CSRF, server-side admin sessions, auth, rate limits,
  Telegram outbox и public product allowlist продолжают покрываться полным набором тестов.
- Таймаут тела — прикладной ASGI deadline, а XLSX CPU deadline остаётся кооперативным. Caddy
  `read_header` защищает заголовки, но внешние CDN/load balancer должны иметь согласованные лимиты.
- Короткий SQLite busy wait сознательно отдаёт retryable 503 вместо удержания worker. Для большой
  нагрузки SQLite и локальные locks всё ещё не заменяют отдельную очередь/СУБД.
- Подробный лог Dependabot может проверить только владелец с write access по URL update
  `1568646066`; без него точная причина внешнего updater failure остаётся неподтверждённой.
- Контейнерную сборку и production TLS/DNS/firewall/runtime необходимо повторить на хосте с
  работающим Docker daemon. Ни один production endpoint в этой работе не сканировался.
