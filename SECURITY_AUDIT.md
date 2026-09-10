# TIGRWEBSITE: исправление безопасности, 10 сентября 2026

Проверка выполнена локально на Windows. Исходный и текущий до правок HEAD совпадали: `f15cc1cd82956a39dba8baa2b5274ed8686085b7`; рабочее дерево было чистым. Все изменения этого отчёта находятся в рабочем дереве, без commit/push/deploy. Рабочие `data` и `.env` не изменялись; тесты используют временные базы, синтетические файлы/контакты и замоканный Telegram.

## Подтверждённые находки и изменения

Ниже подтверждение относится к исходному локальному коду. Факт эксплуатации, реальные production-настройки и установленные там версии пакетов не проверялись. Наличие конкретной CVE production по старым диапазонам requirements не утверждается.

| Проблема | Файл / функция | Исправление | Регрессионное доказательство |
| --- | --- | --- | --- |
| Известные fallback credentials; доверие подписанному флагу | `shop/app.py:create_app`, `shop/security.py:validate_settings` | Явные режимы, fail-closed production, пустой шаблон, Argon2id, проверка параметров и известных/пустых паролей, ограничение ввода до хеширования | `test_unsafe_production_config_fails_before_database_creation`, `test_production_rejects_hash_of_known_default_password`, `test_signed_admin_flag_does_not_grant_authority` |
| Сохранённая cookie оставалась действующей после logout/смены пароля | `shop/security.py`, `shop/database.py`, шаблоны | Непрозрачная cookie, только её хеш в SQLite, idle/absolute expiry, отзыв текущей/всех сессий, версия credentials, серверно подтверждённый интерфейс | `test_logout_revokes_copied_cookie_and_login_rotates_csrf`, `test_admin_session_expires_server_side`, `test_revoke_all_invalidates_other_browser`, `test_credential_rotation_invalidates_old_cookie_on_all_workers` |
| Исходная цена покидала сервер через public API | `shop/app.py:_product_dict`, `_admin_product_dict` | Отдельные явные публичный и административный allowlist | `test_public_allowlist_and_html_escaping` добавляет также будущее служебное поле |
| Только Content-Length; разрешённые файлы на обычных формах | `shop/security.py:SecurityEnvelope`, bounded form parsers, `shop/app.py:_verified_form` | Потоковый ASGI-подсчёт, ограничения полей/файлов/заголовков/типов, 413, закрытие spool при отмене/ошибке; общий допуск импорта до парсинга | `test_streamed_body_limit_without_content_length`, `test_content_length_is_only_early_optimization`, `test_login_checkout_reject_uploaded_files`, `test_http_import_errors_close_all_spooled_files`, `test_multipart_disconnect_and_cancellation_close_spooled_files` |
| Нет серверного ограничения login/checkout | `shop/security.py:consume_rate`, `AdminGate` | Атомарные конечные окна SQLite с HMAC IP, предел 10 000 активных бакетов, 429/Retry-After, только доверенный ASGI client | `test_rate_limit_shared_across_apps_ignores_untrusted_forwarding` |
| Повтор checkout создавал новый заказ | `shop/orders.py:create_checkout`, новые таблицы БД, `templates/checkout.html` | UNIQUE по хешам scope/key, нормализованный payload hash, BEGIN IMMEDIATE; заказ/позиции/outbox одной транзакцией; конфликт 409 | `test_concurrent_http_checkout_replay_conflict_and_new_scope` и конкурентные/rollback-тесты `tests/test_orders_security.py` |
| Синхронный Telegram и транзакции блокировали async endpoint | `shop/orders.py:OutboxWorker`, `shop/telegram.py`, async-маршруты | Ограниченные исполнители; lease, 8 попыток, timeout; соединение SQLite создаётся/закрывается в использующем его потоке | `test_slow_failing_telegram_does_not_block_catalog_health_or_checkout`; тесты конкурирующих и устаревших outbox claims |
| Unicode/NaN/Infinity/слишком большие ID/повреждённый next приводили к ошибкам | `shop/app.py`, `shop/orders.py:normalize_checkout`, `shop/security.py` | NFC, bytes compare, finite/range checks, строгие целочисленные ID/quantity, структура/дубликаты JSON, локальный allowlist redirect | Параметризованные `test_admin_invalid_prices_never_write_or_return_500`, `test_api_rejects_invalid_product_ids`, `test_http_checkout_invalid_numbers_rejected_without_order`, `test_next_redirect_is_controlled` |
| Неверные bytes принимались по расширению изображения; неполные бюджеты импорта | `shop/xlsx_importer.py`, `shop/import_service.py`, CLI | Реальное декодирование Pillow, ZIP/XML/image/work budgets, запрещённые внешние/активные части, атомарная запись, cleanup только новых неиспользуемых файлов, межпроцессные locks и retention | 47 тестов `tests/test_xlsx_security.py`: четыре формата, повторный импорт, ограничения, shared-image cleanup, locks после завершения процесса, 10 файлов и legacy archives |
| Wildcard proxy trust, root app, публичный локальный порт | Dockerfile, оба Compose, Caddy, start-скрипты | UID/GID 10001, loopback development, принудительный production, конкретный IP proxy, caps/resources/read-only/tmpfs, Host allowlist | `test_compose_keeps_app_private_and_trusts_only_proxy`, actual Compose/Caddy validation; runtime Docker ограничения ниже |
| Отсутствовали private cache/referrer headers; терялись exc.headers | `SecurityEnvelope`, HTTPException handler, Caddy | no-store, no-referrer/noindex для заказа, headers также на ошибках, Retry-After сохранён, access-логи отключены; отдельный Caddy handle_errors | `test_private_order_headers_repricing_consent_csv_and_media_path`, `test_host_allowlist_and_404_500_keep_security_headers`, `test_http_exception_preserves_retry_after`, `scripts/check_caddy.py` |
| Нефиксированный граф пакетов, smoke-only CI | requirements input/locks, workflow, Dependabot, scan script | Полный граф с SHA-256, тесты 3.12/3.13, аудит, статический анализ, redacted history scan, SHA-pinned Actions/read-only permissions | hash-checked install, pip check, аудиты и полные pytest ниже |
| Дополнительные ошибки настройки/миграции | setup, database, backup script | BOM-safe rotation; сериализована проверка/ALTER старой схемы; backup API и запрет перезаписи | `tests/test_deployment_security.py`, конкурентный legacy migration test, `test_backup_preserves_committed_wal_and_files_without_overwrite` |

Исходные ограничения ZIP **10 000 записей / 300 MiB** и уже существующие WAL/busy_timeout, HTML-экранирование, параметризованные значения SQL, защита CSV, хешированные имена изображений и защита пути media сохранены. Они не объявлялись отсутствующими. Добавление defusedxml и запрет внешних частей — усиление защиты; воспроизведённых XXE или Zip Slip в исходном коде этот отчёт не заявляет.

## Выполненные команды и фактические результаты

Все пути ниже относительно корня проекта. Для Git использовалось разовое `git -c safe.directory=C:/Users/Denis/Desktop/Tigr/hoco-catalog-shop-github-ready`; глобальный список доверенных каталогов не менялся.

| Команда / проверка | Результат |
| --- | --- |
| `git -c safe.directory=... diff --stat f15cc1cd82956a39dba8baa2b5274ed8686085b7..HEAD` | Пустой diff до изменений; HEAD совпадает со снимком |
| `.\.venv-312\Scripts\python.exe -m pytest -q` | **198 passed**, 2 warnings, 26.96 s; Python 3.12.10 |
| `.\.venv-313\Scripts\python.exe -m pytest -q` | **198 passed**, 2 warnings, 27.03 s; Python 3.13.11 |
| `.\.venv-312\Scripts\python.exe -m compileall -q app.py shop scripts` | PASS, exit 0 |
| `.\.venv-313\Scripts\python.exe -m compileall -q app.py shop scripts` | PASS, exit 0 |
| `.\.venv-312\Scripts\python.exe -m pip check` | `No broken requirements found.` |
| `.\.venv-313\Scripts\python.exe -m pip check` | `No broken requirements found.` |
| `.\.security-tools\bin\uv.exe pip sync requirements-dev.lock --python .venv-312\Scripts\python.exe --require-hashes` | PASS, 59 packages, integrity checks enabled |
| Та же команда с `.venv-313\Scripts\python.exe` | PASS, 59 packages |
| `.\.venv-audit\Scripts\python.exe -m pip_audit --require-hashes --disable-pip -r requirements.lock --progress-spinner off --timeout 15 --cache-dir .uv-cache\pip-audit` | PASS: `No known vulnerabilities found`; все 23 runtime pins сопоставлены с metadata обеих установок 3.12/3.13 |
| Та же команда с `-r requirements-dev.lock` | PASS: `No known vulnerabilities found` для dev-графа |
| `.\.venv-312\Scripts\python.exe -m bandit -r shop scripts app.py` | PASS, exit 0, 0 находок на всех уровнях; предупреждения Bandit о точечных nosec на вложенных AST-узлах |
| `.\.venv-312\Scripts\python.exe scripts/scan_secrets.py --history` | 67 файлов рабочего дерева, 45 исторических blobs, 0 непроверенных кандидатов; пять точечных исключений для известных публичных синтетических примеров |
| `$env:DOMAIN='shop.example.invalid'; docker compose --env-file .env.example -f docker-compose.yml config --no-env-resolution --quiet` | PASS; реальный `.env` не читался и не выводился |
| Та же команда с `-f docker-compose.prod.yml` | PASS |
| `$env:DOMAIN='shop.example.invalid'; .\.security-tools\caddy-release\caddy.exe validate --config Caddyfile.example --adapter caddyfile` | PASS: `Valid configuration`, Caddy v2.11.4; сертификаты/production не запрашивались |
| `.\.venv-312\Scripts\python.exe scripts/check_caddy.py --binary .security-tools/caddy-release/caddy.exe` | **8 checks PASS**: реальный Caddy, 413 для login/checkout/обычной формы с Content-Length и chunked; 502 для admin/заказа при недоступном синтетическом upstream; все защитные headers присутствуют |
| `docker build -t tigr-security-check .` | **NOT RUN**: команда попыталась подключиться к Docker daemon и завершилась до начала сборки; отсутствует работающий daemon |
| Контейнерный runtime: health-check, bind UID, read-only/tmpfs, импорт | **NOT RUN**: требует работающего Docker daemon |
| Реальные TLS/DNS/firewall/proxy, production данные и логи | **NOT RUN**: развёртывание/активные production-проверки не входили в локальную работу |

Две предупреждающие записи pytest принадлежат upstream Starlette TestClient: переход от httpx к httpx2 и устаревший alias AnyIO BlockingPortal. Тесты успешно исполняются с согласованным lock; предупреждения не скрывались. Промежуточные обнаруженные ошибки лимитов и миграции были исправлены и включены в регрессии. Первые диагностические прогоны на установленном Python 3.10 не заменяют итоговую проверку поддерживаемых 3.12/3.13.

Локальный pip-audit запущен в отдельном служебном окружении Python 3.10: переносимые интерпретаторы 3.12/3.13 не содержат stdlib `venv`, которую импортирует сам аудитор. Входной runtime lock содержит 23 точных версии; их полное совпадение с установленными metadata обоих целевых окружений проверено отдельно. Аудит не использовался для утверждений о неизвестных production-пакетах. Основные зафиксированные версии: FastAPI 0.141.1, Starlette 1.6.0, Uvicorn 0.52.4, python-multipart 0.0.32, Pillow 12.3.0, argon2-cffi 25.1.0.

SHA Actions сверены read-only с официальными GitHub tag refs: checkout v4.2.2 — `11bd71901bbe5b1630ceea73d27597364c9af683`, setup-python v5.6.0 — `a26af69be951a213d495a4c3e4e4022e16d87065`. Архив Caddy 2.11.4 проверен по SHA-256 официального release asset: `1708333f79e274c7697285afe6d592ab39314e0b131e9ec6bea08ad27df62ebf`.

Статические B608 исключены адресно только там, где SQL строится из констант/allowlist и количества placeholders, а значения передаются отдельно. B310 относится к фиксированному HTTPS-хосту Telegram с timeout и ограничением ответа; B405 — к использованию ElementTree только для типов/ParseError (парсит defusedxml). Subprocess secret scanner запускает найденный git без shell, с фиксированными read-only операциями. Нет общего подавления класса уязвимостей или маскирующего `except Exception` в обработке пользовательских форм; широкая граница внешнего Telegram не раскрывает текст сетевой ошибки с credentials.

Проверка Caddy использует исходный Caddyfile с заменой listener/upstream на временные loopback-адреса и отключёнными automatic HTTPS/admin для теста. Сначала воспроизведено отсутствие headers на собственных 413/502 Caddy; после добавления handle_errors все восемь проверок прошли. Это проверка HTTP-обработчиков реального бинарного файла, не проверка production TLS или Docker-сети.

## Миграция, backup и откат

Миграция добавляет `admin_sessions`, `security_state`, `rate_limits`, `checkout_requests`, `notification_outbox`; существующие записи каталога, заказов, позиций, consent и public_token сохраняются. Старым заказам не создаются неожиданные Telegram-задания. Проверка недостающих consent-полей/ALTER выполняется под BEGIN IMMEDIATE, повторный и параллельный старт протестированы.

Перед обновлением остановите web и CLI, выполните `python scripts/backup_data.py --data-dir data --destination ../tigr-backup-2026-09-10` в новый приватный каталог, отдельно сохраните `.env`/Caddy. Проверяется SQLite integrity; копия содержит committed WAL-данные и media/imports. При восстановлении держите процессы остановленными, сохраните текущие данные отдельно и не смешивайте DB с WAL/SHM другого снимка. Откат backup теряет более новые заказы без отдельного переноса. Исходную уязвимую версию не возвращайте в публичный доступ. Полная процедура и предупреждения о правах находятся в README.

## Действия владельца

1. До запуска обновлённого production выполнить `python scripts/setup_security.py --rotate`, задать новый уникальный пароль и перезапустить все worker с одной конфигурацией. Старая `.env` с ADMIN_PASSWORD должна быть мигрирована. Реальные credentials этой рабочей сессией не заменялись.
2. Настроить DOMAIN, Host allowlist, DNS/HTTPS; проверить выделенную proxy-сеть и отсутствие прямого доступа к порту приложения. Не включать wildcard trust.
3. Проверить UID/GID 10001 на конкретных data/DB/WAL/SHM/подкаталогах, приватность `.env`/backup, доступность диска и корректный реальный импорт после backup. Не применять рекурсивное изменение прав к чужим каталогам.
4. Запустить Docker-сборку и контейнерные проверки на машине с daemon; проверить внешние логи/CDN/APM на отсутствие bearer URL и персональных данных.
5. Указать реквизиты оператора/политику, контролировать очередь failed, рост media и старых архивов, регулярно обновлять и аудитировать lock.

## Остаточные риски

- Bearer-ссылка заказа сохраняет прежний бессрочный доступ; её утечка требует отдельной политики отзыва, которую это совместимое изменение не вводит.
- Telegram может продублировать доставку после неопределённого ответа; после 8 попыток требуется разбор failed-заданий. Ошибки не откатывают заказ.
- SQLite и лимиты по IP подходят масштабу проекта, но не заменяют защиту от распределённого отказа в обслуживании; общий NAT делит лимит. Пиковую ёмкость production не измеряли.
- XLSX deadline кооперативный. Жёсткие пределы памяти/диска дополняются Docker/ОС; при аварийном завершении могут остаться ограниченные бюджетом импорта новые изображения. Успешные старые версии media и прежние unmanaged архивы требуют осознанной политики очистки; shared-файлы автоматически не удаляются.
- Отключение runtime request-логов Caddy снижает диагностическую видимость. Пользовательская инфраструктура логирования требует отдельной проверки.
- Python-пакеты зафиксированы с хешами; OS/base-image слои не объявляются полностью воспроизводимыми, образ Python используется по версии/tag. Контейнерная сборка здесь не подтверждена.
- Локальные portable Python/служебные окружения находятся в игнорируемых `.python`, `.venv-*`, `.security-tools`, `.uv-cache`; это инструменты проверки, не production-runtime и не часть Git/Docker build context.
