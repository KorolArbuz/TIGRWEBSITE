from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
import secrets
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from shop.app import create_app
from shop.database import connect
from shop.security import ADMIN_COOKIE, FORM_BODY_BYTES, LOGIN_BODY_BYTES


def hidden(response, name):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', response.text)
    assert match is not None
    return match.group(1)


def login(client, password, *, next_url="/admin"):
    csrf = hidden(client.get("/admin/login"), "csrf_token")
    response = client.post("/admin/login", data={"csrf_token": csrf, "password": password, "next": next_url})
    assert response.status_code == 303
    assert client.cookies.get(ADMIN_COOKIE) is not None
    return response


def checkout_data(client, **changes):
    page = client.get("/checkout")
    assert page.status_code == 200
    data = {
        "csrf_token": hidden(page, "csrf_token"),
        "idempotency_key": hidden(page, "idempotency_key"),
        "customer_name": "Synthetic buyer",
        "phone": "0000000000",
        "personal_data_consent": "1",
        "cart_json": '[{"id":1,"quantity":2}]',
    }
    data.update(changes)
    return data


def security_headers(headers):
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.mark.parametrize("changes", [
    {"SECRET_KEY": ""}, {"SECRET_KEY": "dev-secret-change-before-public-launch"},
    {"SECRET_KEY": "a" * 80}, {"SECRET_KEY": "replace-this-example-with-a-new-secret-key-locally"},
    {"SECRET_KEY": "0123456789abcdef" * 4},
    {"SECRET_KEY": string.ascii_lowercase + string.ascii_uppercase + string.digits + "-_"},
    {"ADMIN_PASSWORD_HASH": ""}, {"ADMIN_PASSWORD": "change-me-now"},
    {"SESSION_COOKIE_SECURE": False}, {"APP_ENV": "unknown"}, {"ALLOWED_HOSTS": "*"},
])
def test_unsafe_production_config_fails_before_database_creation(app_config, changes):
    settings = {**app_config, "APP_ENV": "production", "SESSION_COOKIE_SECURE": True, **changes}
    assert not Path(settings["DATABASE_PATH"]).exists()
    with pytest.raises(ValueError):
        create_app(settings)
    assert not Path(settings["DATABASE_PATH"]).exists()


@pytest.mark.parametrize("password", ["change-me-now", ""], ids=["known-default", "empty"])
def test_production_rejects_hash_of_known_default_password(app_config, password):
    weak_hash = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(password)
    assert not Path(app_config["DATABASE_PATH"]).exists()
    with pytest.raises(ValueError):
        create_app({**app_config, "APP_ENV": "production", "SESSION_COOKIE_SECURE": True,
                    "ADMIN_PASSWORD_HASH": weak_hash})
    assert not Path(app_config["DATABASE_PATH"]).exists()


def test_secure_production_cookie_is_opaque_and_http_only(app_config, admin_credentials):
    app = create_app({**app_config, "APP_ENV": "production", "SESSION_COOKIE_SECURE": True})
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        response = login(client, admin_credentials[0])
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(ADMIN_COOKIE + "="))
        assert bool(re.search(r"; secure(?:;|$)", cookie, re.I))
        assert bool(re.search(r"; httponly(?:;|$)", cookie, re.I))
        assert "samesite=strict" in cookie.lower()
        token = client.cookies.get(ADMIN_COOKIE)
        assert bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", token))
        with closing(connect(app_config["DATABASE_PATH"])) as db:
            row = db.execute("SELECT token_hash FROM admin_sessions").fetchone()
            assert bool(row and row[0] != token and len(row[0]) == 64)


@pytest.mark.parametrize("signing_key", ["dev-secret-change-before-public-launch", "current"])
def test_signed_admin_flag_does_not_grant_authority(client, app_config, signing_key):
    key = app_config["SECRET_KEY"] if signing_key == "current" else signing_key
    payload = base64.b64encode(json.dumps({"admin_authenticated": True}).encode())
    forged = TimestampSigner(key).sign(payload).decode()
    response = client.get("/admin/export/orders.csv", headers={"Cookie": "session=" + forged})
    assert response.status_code == 303
    page = client.get("/", headers={"Cookie": "session=" + forged})
    assert "Админка" not in page.text


def test_every_admin_route_requires_server_authorization(app, client):
    protected = [route for route in app.routes if getattr(route, "path", "").startswith("/admin")
                 and route.path != "/admin/login"]
    assert protected
    for route in protected:
        path = route.path.replace("{order_id}", "1").replace("{product_id}", "1")
        for method in route.methods:
            response = client.request(method, path)
            assert response.status_code == 303, (method, path)
            assert response.headers["location"] == "/admin/login"
            assert response.headers["cache-control"] == "no-store"
            security_headers(response.headers)


def test_every_admin_mutation_requires_csrf(app, client, admin_credentials):
    login(client, admin_credentials[0])
    for route in app.routes:
        if not getattr(route, "path", "").startswith("/admin") or "POST" not in getattr(route, "methods", set()):
            continue
        path = route.path.replace("{order_id}", "1").replace("{product_id}", "1")
        response = client.post(path, data={"csrf_token": "synthetic-invalid"})
        assert response.status_code == 400, path
    assert client.get("/admin").status_code == 200


def test_logout_revokes_copied_cookie_and_login_rotates_csrf(client, app_config, admin_credentials):
    previous = hidden(client.get("/admin/login"), "csrf_token")
    login(client, admin_credentials[0])
    saved = client.cookies.get(ADMIN_COOKIE)
    current = hidden(client.get("/admin"), "csrf_token")
    assert bool(current != previous)
    response = client.post("/admin/logout", data={"csrf_token": current})
    assert response.status_code == 303
    assert client.get("/admin", headers={"Cookie": ADMIN_COOKIE + "=" + saved}).status_code == 303
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert db.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_revoke_all_invalidates_other_browser(app, client, admin_credentials):
    login(client, admin_credentials[0])
    with TestClient(app, follow_redirects=False) as second:
        login(second, admin_credentials[0])
        assert second.get("/admin").status_code == 200
        csrf = hidden(client.get("/admin"), "csrf_token")
        assert client.post("/admin/sessions/revoke", data={"csrf_token": csrf}).status_code == 303
        assert second.get("/admin").status_code == 303


@pytest.mark.parametrize("expiry", ["idle", "absolute"])
def test_admin_session_expires_server_side(client, app_config, admin_credentials, expiry):
    login(client, admin_credentials[0])
    with closing(connect(app_config["DATABASE_PATH"])) as db, db:
        if expiry == "idle":
            db.execute("UPDATE admin_sessions SET last_seen = 0")
        else:
            db.execute("UPDATE admin_sessions SET expires_at = 0")
    assert client.get("/admin").status_code == 303


@pytest.mark.parametrize("credential", ["password", "secret_key"])
def test_credential_rotation_invalidates_old_cookie_on_all_workers(client, app_config, admin_credentials, credential):
    login(client, admin_credentials[0])
    saved = client.cookies.get(ADMIN_COOKIE)
    if credential == "password":
        replacement_hash = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(secrets.token_urlsafe(32))
        changes = {"ADMIN_PASSWORD_HASH": replacement_hash}
    else:
        changes = {"SECRET_KEY": secrets.token_urlsafe(48)}
    replacement = create_app({**app_config, **changes})
    with TestClient(replacement, follow_redirects=False) as new_client:
        assert new_client.get("/admin", headers={"Cookie": ADMIN_COOKIE + "=" + saved}).status_code == 303
    assert client.get("/admin").status_code == 303


def test_guest_checkout_scope_survives_admin_login(client, admin_credentials):
    data = checkout_data(client)
    login(client, admin_credentials[0])
    assert client.post("/checkout", data=data).status_code == 400  # Old CSRF was rotated.
    data["csrf_token"] = hidden(client.get("/checkout"), "csrf_token")
    assert client.post("/checkout", data=data).status_code == 303


def test_public_allowlist_and_html_escaping(client, app_config):
    with closing(connect(app_config["DATABASE_PATH"])) as db, db:
        db.execute("ALTER TABLE products ADD COLUMN future_private TEXT DEFAULT 'synthetic-private-value'")
        db.execute("UPDATE products SET model = ?", ('<script>alert("synthetic")</script>',))
    response = client.get("/api/products", params={"ids": "1"})
    assert response.status_code == 200
    product = response.json()["products"][0]
    assert set(product) <= {"id", "barcode", "brand", "category", "model", "color", "box_qty", "price_cents",
                            "description", "badge", "images", "image", "active", "title"}
    for page in (response, client.get("/"), client.get("/product/1")):
        assert "source_price_cents" not in page.text
        assert "private-synthetic-source" not in page.text
        assert "private-synthetic-sheet" not in page.text
        assert "synthetic-private-value" not in page.text
    catalog = client.get("/").text
    assert '<script>alert("synthetic")</script>' not in catalog
    assert "&lt;script&gt;" in catalog


@pytest.mark.parametrize("path", ["/admin/login", "/checkout"])
def test_login_checkout_reject_uploaded_files(client, path):
    csrf = hidden(client.get(path), "csrf_token")
    response = client.post(path, data={"csrf_token": csrf}, files={"files": ("synthetic.txt", b"synthetic", "text/plain")})
    assert response.status_code == 400
    security_headers(response.headers)


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "application/octet-stream"])
def test_form_content_type_allowlist(client, content_type):
    client.get("/checkout")
    assert client.post("/checkout", content=b"{}", headers={"Content-Type": content_type}).status_code == 415


def test_duplicate_fields_and_unicode_csrf_are_controlled(client):
    csrf = hidden(client.get("/checkout"), "csrf_token")
    body = urlencode([("csrf_token", csrf), ("csrf_token", csrf)])
    assert client.post("/checkout", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.post("/checkout", data={"csrf_token": "неверный-🍉"}).status_code == 400


def test_urlencoded_field_and_count_budgets_return_413(client):
    csrf = hidden(client.get("/checkout"), "csrf_token")
    oversized = client.post("/checkout", data={"csrf_token": csrf, "comment": "x" * 17000})
    assert oversized.status_code == 413
    too_many = client.post("/checkout", data={"csrf_token": csrf, **{f"synthetic{n}": "x" for n in range(21)}})
    assert too_many.status_code == 413
    security_headers(oversized.headers)
    security_headers(too_many.headers)


async def raw_request(app, *, path, chunks, cookie="", content_length=None):
    headers = [(b"host", b"testserver"), (b"content-type", b"application/x-www-form-urlencoded")]
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": headers, "client": ("192.0.2.20", 10000), "server": ("testserver", 80)}
    messages = []
    consumed = 0

    async def receive():
        nonlocal consumed
        if consumed >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[consumed]
        consumed += 1
        return {"type": "http.request", "body": chunk, "more_body": consumed < len(chunks)}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], {key.decode(): value.decode() for key, value in start["headers"]}, consumed


@pytest.mark.parametrize("path,limit", [("/admin/login", LOGIN_BODY_BYTES), ("/checkout", FORM_BODY_BYTES)])
def test_streamed_body_limit_without_content_length(app, client, path, limit):
    csrf = hidden(client.get(path), "csrf_token")
    cookie = "session=" + client.cookies.get("session")
    prefix = urlencode({"csrf_token": csrf}).encode()
    chunks = [prefix] + [f"&field{number}=".encode() + b"x" * 4096 for number in range(limit // 4096 + 8)]
    status, headers, consumed = asyncio.run(raw_request(app, path=path, chunks=chunks, cookie=cookie))
    assert status == 413
    assert consumed < len(chunks)
    security_headers(headers)


def test_content_length_is_only_early_optimization(app, client):
    csrf = hidden(client.get("/admin/login"), "csrf_token")
    cookie = "session=" + client.cookies.get("session")
    status, headers, _ = asyncio.run(raw_request(app, path="/admin/login", chunks=[urlencode({"csrf_token": csrf}).encode(),
                                                                             b"&password=" + b"x" * LOGIN_BODY_BYTES],
                                               cookie=cookie, content_length=b"1"))
    assert status == 413
    security_headers(headers)


def test_host_allowlist_and_404_500_keep_security_headers(app, client):
    assert client.get("/", headers={"Host": "untrusted.invalid"}).status_code == 400
    security_headers(client.get("/missing-synthetic-route").headers)

    @app.get("/synthetic-failure")
    def synthetic_failure():
        raise RuntimeError("synthetic failure")

    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as safe_client:
        response = safe_client.get("/synthetic-failure")
        assert response.status_code == 500
        security_headers(response.headers)


def test_http_exception_preserves_retry_after(app, client):
    @app.get("/api/synthetic-retry")
    def synthetic_retry():
        raise HTTPException(429, "synthetic retry", headers={"Retry-After": "7"})

    response = client.get("/api/synthetic-retry")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    security_headers(response.headers)


@pytest.mark.parametrize("path,setting", [("/admin/login", "LOGIN_RATE_LIMIT"), ("/checkout", "CHECKOUT_RATE_LIMIT")])
def test_rate_limit_shared_across_apps_ignores_untrusted_forwarding(app_config, path, setting):
    settings = {**app_config, setting: 2}
    first = create_app(settings)
    second = create_app(settings)
    with TestClient(first, follow_redirects=False) as client, TestClient(second, follow_redirects=False) as other:
        csrf = hidden(client.get(path), "csrf_token")
        for number in range(2):
            response = client.post(path, data={"csrf_token": csrf}, headers={"X-Forwarded-For": f"192.0.2.{number + 1}"})
            assert response.status_code != 429
        csrf_other = hidden(other.get(path), "csrf_token")
        response = other.post(path, data={"csrf_token": csrf_other}, headers={"X-Forwarded-For": "198.51.100.70", "Forwarded": "for=198.51.100.71"})
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        security_headers(response.headers)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999999", "sNaN", "9" * 100])
def test_admin_invalid_prices_never_write_or_return_500(client, app_config, admin_credentials, value):
    login(client, admin_credentials[0])
    csrf = hidden(client.get("/admin"), "csrf_token")
    response = client.post("/admin/products/1", data={"csrf_token": csrf, "price": value, "active": "1"})
    assert response.status_code in {400, 303}
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert db.execute("SELECT price_cents FROM products WHERE id=1").fetchone()[0] == 12345


@pytest.mark.parametrize("value", ["true", "1.5", "NaN", "Infinity", str(2**63), "9" * 500, "-1"])
def test_api_rejects_invalid_product_ids(client, value):
    assert client.get("/api/products", params={"ids": value}).status_code == 400


def test_cart_sync_deduplicates_repeated_product_ids(client):
    response = client.get("/api/products", params={"ids": "1,1"})
    assert response.status_code == 200
    assert len(response.json()["products"]) == 1


@pytest.mark.parametrize("value", ["http://[", "//untrusted.invalid", "/admin-evil", "/admin/../checkout", "/admin\\evil",
                                  "https://untrusted.invalid/admin", "/admin%2f%2funtrusted.invalid", "/admin\r\nX-Test:evil"])
def test_next_redirect_is_controlled(client, admin_credentials, value):
    response = login(client, admin_credentials[0], next_url=value)
    assert response.headers["location"] == "/admin"


def test_unicode_login_and_oversized_password_controlled(client, monkeypatch):
    import shop.security as security

    csrf = hidden(client.get("/admin/login"), "csrf_token")
    response = client.post("/admin/login", data={"csrf_token": csrf, "password": "неверный-пароль-🍉"})
    assert response.status_code == 303
    assert client.cookies.get(ADMIN_COOKIE) is None
    checked = []

    def reject_call(*args):
        checked.append(1)
        return False

    monkeypatch.setattr(type(security.PASSWORD_HASHER), "verify", reject_call)
    response = client.post("/admin/login", data={"csrf_token": csrf, "password": "x" * 1025})
    assert response.status_code == 303
    assert checked == []


@pytest.mark.parametrize("cart", ['[{"id":true,"quantity":1}]', '[{"id":1.1,"quantity":1}]',
                                  '[{"id":1,"quantity":1.5}]', '[{"id":1,"quantity":NaN}]',
                                  '[{"id":Infinity,"quantity":1}]', '[{"id":9223372036854775808,"quantity":1}]'])
def test_http_checkout_invalid_numbers_rejected_without_order(client, app_config, cart):
    data = checkout_data(client, cart_json=cart)
    assert client.post("/checkout", data=data).status_code == 400
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_concurrent_http_checkout_replay_conflict_and_new_scope(app, client, app_config):
    data = checkout_data(client)
    barrier = threading.Barrier(4)

    def submit(_):
        barrier.wait(timeout=10)
        return client.post("/checkout", data=data)

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(submit, range(4)))
    assert all(response.status_code == 303 for response in responses)
    assert bool(len({response.headers["location"] for response in responses}) == 1)
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("orders", "order_items", "notification_outbox")) == (1, 1, 1)
    assert client.post("/checkout", data={**data, "comment": "Different synthetic comment"}).status_code == 409
    assert client.post("/checkout", data={**data, "idempotency_key": secrets.token_urlsafe(32)}).status_code == 303
    with TestClient(app, follow_redirects=False) as other:
        other_data = checkout_data(other, idempotency_key=data["idempotency_key"])
        assert other.post("/checkout", data=other_data).status_code == 303
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_private_order_headers_repricing_consent_csv_and_media_path(client, app_config, admin_credentials):
    data = checkout_data(client, customer_name="=SYNTHETIC()", cart_json='[{"id":1,"quantity":2,"price_cents":1}]')
    result = client.post("/checkout", data=data)
    assert result.status_code == 303
    page = client.get(result.headers["location"])
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in page.headers["x-robots-tag"]
    security_headers(page.headers)
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        row = db.execute("SELECT total_cents,personal_data_consent,privacy_policy_version FROM orders").fetchone()
        assert tuple(row) == (24690, 1, "synthetic-policy-v1")
    login(client, admin_credentials[0])
    exported = client.get("/admin/export/orders.csv")
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    values = list(csv.reader(io.StringIO(exported.text), delimiter=";"))
    assert values[1][3] == "'=SYNTHETIC()"
    assert client.get("/media/%2e%2e%2fsynthetic-http.sqlite3").status_code == 404


def test_slow_failing_telegram_does_not_block_catalog_health_or_checkout(app_config, monkeypatch):
    import shop.orders as orders

    entered, release = threading.Event(), threading.Event()

    def slow_sender(*args):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("synthetic sender deadline")
        raise RuntimeError("synthetic delivery failure")

    monkeypatch.setattr(orders, "send_order_notification", slow_sender)
    app = create_app({**app_config, "TELEGRAM_BOT_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "synthetic-chat"})
    with closing(connect(app_config["DATABASE_PATH"])) as db, db:
        db.execute("INSERT INTO products(barcode,brand,model,source_price_cents,price_cents) VALUES ('synthetic','Example','Product',1,100)")
    with TestClient(app, follow_redirects=False) as client:
        try:
            assert client.post("/checkout", data=checkout_data(client)).status_code == 303
            assert entered.wait(timeout=5)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(client.get, path) for path in ("/", "/healthz")]
                assert all(future.result(timeout=2).status_code == 200 for future in futures)
            assert not release.is_set()
        finally:
            release.set()
    with closing(connect(app_config["DATABASE_PATH"])) as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert tuple(db.execute("SELECT status,attempts FROM notification_outbox").fetchone()) == ("pending", 1)


@pytest.fixture
def recorded_spools(monkeypatch, tmp_path):
    import starlette.formparsers as formparsers

    original = formparsers.SpooledTemporaryFile
    opened = []

    def recording_file(*args, **kwargs):
        file = original(*args, dir=tmp_path, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_file)
    return opened


def multipart_file_prefix():
    return (b'--synthetic-boundary\r\nContent-Disposition: form-data; name="files"; filename="synthetic.xlsx"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nsynthetic bytes')


@pytest.mark.parametrize("failure,expected", [("truncated", 400), ("malformed", 400), ("csrf", 400),
                                              ("field_size", 413), ("header_size", 413), ("file_count", 413)])
def test_http_import_errors_close_all_spooled_files(client, admin_credentials, recorded_spools, failure, expected):
    login(client, admin_credentials[0])
    csrf = hidden(client.get("/admin"), "csrf_token")
    body = multipart_file_prefix()
    boundary = b"\r\n--synthetic-boundary\r\n"
    ending = b"\r\n--synthetic-boundary--\r\n"
    csrf_part = b'Content-Disposition: form-data; name="csrf_token"\r\n\r\n' + csrf.encode()
    if failure == "malformed":
        body += boundary + b"invalid header\r\n\r\n" + ending
    elif failure == "csrf":
        body += boundary + b'Content-Disposition: form-data; name="csrf_token"\r\n\r\nwrong' + ending
    elif failure == "field_size":
        body += boundary + b'Content-Disposition: form-data; name="comment"\r\n\r\n' + b"x" * 17000 + ending
    elif failure == "header_size":
        body += boundary + b'Content-Disposition: form-data; name="' + b"x" * 17000 + b'"\r\n\r\n' + ending
    elif failure == "file_count":
        body += (b"\r\n" + multipart_file_prefix()) * 10 + boundary + csrf_part + ending
    response = client.post("/admin/import", content=body,
                           headers={"Content-Type": "multipart/form-data; boundary=synthetic-boundary"})
    assert response.status_code == expected
    assert recorded_spools
    assert all(file.closed for file in recorded_spools)
    security_headers(response.headers)


@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
def test_multipart_disconnect_and_cancellation_close_spooled_files(recorded_spools, failure):
    from starlette.datastructures import Headers
    from starlette.requests import ClientDisconnect

    from shop.security import BoundedMultiPartParser

    async def stream():
        yield multipart_file_prefix()
        if failure == "disconnect":
            raise ClientDisconnect()
        raise asyncio.CancelledError()

    async def parse():
        parser = BoundedMultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=synthetic-boundary"}),
                                        stream(), max_files=10, max_fields=10, max_part_size=16384)
        await parser.parse()

    with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
        asyncio.run(parse())
    assert recorded_spools
    assert all(file.closed for file in recorded_spools)


def test_streamed_per_file_limit_closes_spool_before_unbounded_disk_growth(recorded_spools):
    from starlette.datastructures import Headers

    from shop.security import BoundedMultiPartParser, MultipartBudgetExceeded

    consumed = 0

    async def stream():
        nonlocal consumed
        yield multipart_file_prefix()
        chunk = b"x" * (1024 * 1024)
        for _ in range(110):
            consumed += 1
            yield chunk

    async def parse():
        parser = BoundedMultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=synthetic-boundary"}),
                                        stream(), max_files=10, max_fields=10, max_part_size=16384)
        await parser.parse()

    with pytest.raises(MultipartBudgetExceeded):
        asyncio.run(parse())
    assert consumed <= 100
    assert recorded_spools
    assert all(file.closed for file in recorded_spools)


@pytest.mark.parametrize("path", ["/media/%00synthetic", "/media/" + "x" * 300,
                                  "/media/%2e%2e%5csynthetic-http.sqlite3", "/media/%FF%FE"])
def test_malformed_media_paths_return_controlled_client_error(client, path):
    assert 400 <= client.get(path).status_code < 500
