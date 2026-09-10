"""Configuration, revocable administrative authority and bounded ASGI input."""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
import unicodedata
from contextlib import closing
from typing import Any

import anyio
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from .database import connect
from .import_service import ImportBusyError, upload_slot

ADMIN_COOKIE = "tigr_admin"
PASSWORD_HASHER = PasswordHasher()
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
LOGIN_BODY_BYTES = 16 * 1024
FORM_BODY_BYTES = 64 * 1024
IMPORT_BODY_BYTES = 128 * 1024 * 1024


def password_bytes(value: str) -> bytes:
    return unicodedata.normalize("NFC", value).encode("utf-8")


def verify_password(encoded_hash: str, supplied: str) -> bool:
    if not isinstance(supplied, str) or len(supplied) > 1024:
        return False
    try:
        value = password_bytes(supplied)
    except UnicodeError:
        return False
    if not value or len(value) > 1024:
        return False
    try:
        return PASSWORD_HASHER.verify(encoded_hash, value)
    except (VerificationError, InvalidHashError):
        return False


def validate_settings(settings: dict[str, Any]) -> None:
    mode = settings["APP_ENV"]
    if mode not in {"production", "development", "test"}:
        raise ValueError("APP_ENV must be production, development or test")
    secret = str(settings["SECRET_KEY"])
    lowered = secret.lower()
    repeated = any(secret == (secret[:size] * (len(secret) // size + 1))[:len(secret)]
                   for size in range(1, min(32, len(secret) // 2) + 1))
    public_alphabets = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    )
    if (len(secret) < 43 or len(secret) > 512 or len(set(secret)) < 12
            or repeated or any(secret in alphabet * 8 for alphabet in public_alphabets)
            or any(char.isspace() for char in secret)
            or any(word in lowered for word in ("change", "example", "placeholder", "dev-secret", "your-secret", "replace", "test-secret"))):
        raise ValueError("SECRET_KEY must be a locally generated high-entropy secret")
    if mode == "production" and settings["SESSION_COOKIE_SECURE"] is not True:
        raise ValueError("Production requires SESSION_COOKIE_SECURE=true")
    if settings.get("ADMIN_PASSWORD"):
        raise ValueError("Remove ADMIN_PASSWORD and configure ADMIN_PASSWORD_HASH using setup")
    encoded_hash = str(settings.get("ADMIN_PASSWORD_HASH", ""))
    try:
        params = extract_parameters(encoded_hash)
    except (InvalidHashError, ValueError) as exc:
        raise ValueError("ADMIN_PASSWORD_HASH must contain an Argon2id hash") from exc
    if (params.type != Type.ID or params.version != 19 or not 19456 <= params.memory_cost <= 262144
            or not 2 <= params.time_cost <= 6 or not 1 <= params.parallelism <= 8
            or params.salt_len < 16 or params.hash_len < 32):
        raise ValueError("ADMIN_PASSWORD_HASH parameters are outside the supported safety limits")
    for known in (b"", b"change-me-now", b"admin", b"password", b"password123", b"change-me", b"12345678"):
        try:
            is_known = PASSWORD_HASHER.verify(encoded_hash, known)
        except VerifyMismatchError:
            is_known = False
        except (InvalidHashError, VerificationError) as exc:
            raise ValueError("Invalid ADMIN_PASSWORD_HASH") from exc
        if is_known:
            raise ValueError("The administrative password is a known default")
    hosts = settings["ALLOWED_HOSTS"]
    if isinstance(hosts, str):
        hosts = [host.strip() for host in hosts.split(",") if host.strip()]
    host_pattern = r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    if not hosts or any(not isinstance(host, str) or not re.fullmatch(host_pattern, host) for host in hosts):
        raise ValueError("ALLOWED_HOSTS requires explicit hostnames without schemes, ports or wildcards")
    settings["ALLOWED_HOSTS"] = [host.lower() for host in hosts]
    for name, default, maximum in (
        ("ADMIN_IDLE_SECONDS", 1800, 86400), ("ADMIN_ABSOLUTE_SECONDS", 28800, 604800),
        ("LOGIN_RATE_LIMIT", 20, 10000), ("LOGIN_RATE_WINDOW", 300, 86400),
        ("CHECKOUT_RATE_LIMIT", 60, 10000), ("CHECKOUT_RATE_WINDOW", 600, 86400),
    ):
        try:
            value = int(settings.get(name, default))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid {name}") from exc
        if not 1 <= value <= maximum:
            raise ValueError(f"Invalid {name}")
        settings[name] = value
    settings["CREDENTIAL_VERSION"] = hmac.new(
        secret.encode("utf-8"), encoded_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sync_credentials(settings: dict[str, Any]) -> None:
    with closing(connect(settings["DATABASE_PATH"])) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM security_state WHERE key='admin_credentials'").fetchone()
        if row is None or row[0] != settings["CREDENTIAL_VERSION"]:
            db.execute("DELETE FROM admin_sessions")
            db.execute("INSERT INTO security_state(key,value) VALUES ('admin_credentials',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (settings["CREDENTIAL_VERSION"],))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_admin_session(settings: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with closing(connect(settings["DATABASE_PATH"])) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("SELECT value FROM security_state WHERE key='admin_credentials'").fetchone()
        if version is None or version[0] != settings["CREDENTIAL_VERSION"]:
            raise RuntimeError("Administrative credentials changed; restart this worker")
        db.execute("DELETE FROM admin_sessions WHERE expires_at<=? OR last_seen<=?",
                   (now, now - settings["ADMIN_IDLE_SECONDS"]))
        # Bound state even after repeated successful authentication.
        db.execute("DELETE FROM admin_sessions WHERE token_hash IN (SELECT token_hash FROM admin_sessions "
                   "ORDER BY created_at DESC LIMIT -1 OFFSET 99)")
        db.execute("INSERT INTO admin_sessions VALUES (?,?,?,?,?)", (
            _token_hash(token), now, now, now + settings["ADMIN_ABSOLUTE_SECONDS"], settings["CREDENTIAL_VERSION"]
        ))
    return token


def authenticated(settings: dict[str, Any], token: str) -> bool:
    if not TOKEN_RE.fullmatch(token):
        return False
    now = time.time()
    with closing(connect(settings["DATABASE_PATH"])) as db, db:
        cursor = db.execute(
            "UPDATE admin_sessions SET last_seen=? WHERE token_hash=? AND expires_at>? AND last_seen>? "
            "AND credential_version=? AND credential_version="
            "(SELECT value FROM security_state WHERE key='admin_credentials')",
            (now, _token_hash(token), now, now - settings["ADMIN_IDLE_SECONDS"], settings["CREDENTIAL_VERSION"]),
        )
        return cursor.rowcount == 1


def revoke_sessions(settings: dict[str, Any], token: str | None = None) -> None:
    with closing(connect(settings["DATABASE_PATH"])) as db, db:
        if token is None:
            db.execute("DELETE FROM admin_sessions")
        elif TOKEN_RE.fullmatch(token):
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_token_hash(token),))


def consume_rate(settings: dict[str, Any], action: str, client: str) -> int:
    """Atomic fixed windows, shared across processes; no raw client IP retained."""
    now = time.time()
    prefix = "LOGIN" if action == "login" else "CHECKOUT"
    window = settings[f"{prefix}_RATE_WINDOW"]
    limit = settings[f"{prefix}_RATE_LIMIT"]
    bucket = action + ":" + hmac.new(settings["SECRET_KEY"].encode(), client.encode(), hashlib.sha256).hexdigest()
    with closing(connect(settings["DATABASE_PATH"])) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
        row = db.execute("SELECT count,expires_at FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()
        if row is not None:
            if row[0] >= limit:
                return max(1, math.ceil(row[1] - now))
            db.execute("UPDATE rate_limits SET count=count+1 WHERE bucket=?", (bucket,))
        else:
            if db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] >= 10000:
                return max(1, math.ceil(db.execute("SELECT MIN(expires_at) FROM rate_limits").fetchone()[0] - now))
            db.execute("INSERT INTO rate_limits VALUES (?,?,1,?)", (bucket, now, now + window))
    return 0


class AdminGate:
    def __init__(self, app: Any, settings: dict[str, Any]) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        request.state.admin_authenticated = await anyio.to_thread.run_sync(
            authenticated, self.settings, request.cookies.get(ADMIN_COOKIE, "")
        )
        path = scope["path"]
        if (path == "/admin" or path.startswith("/admin/")) and path != "/admin/login":
            if not request.state.admin_authenticated:
                await RedirectResponse("/admin/login", status_code=303)(scope, receive, send)
                return
        if scope["method"] == "POST" and path in {"/admin/login", "/checkout"}:
            # Uvicorn resolves trusted proxies; never inspect arbitrary forwarding headers here.
            client = scope.get("client") or ("unknown", 0)
            retry = await anyio.to_thread.run_sync(
                consume_rate, self.settings, "login" if path == "/admin/login" else "checkout", str(client[0])
            )
            if retry:
                await PlainTextResponse("Слишком много запросов. Повторите позже.", 429,
                                        headers={"Retry-After": str(retry)})(scope, receive, send)
                return
        if path == "/admin/import" and scope["method"] == "POST":
            slot = upload_slot(self.settings["UPLOAD_ROOT"])
            try:
                await anyio.to_thread.run_sync(slot.__enter__)
            except ImportBusyError:
                await PlainTextResponse("Импорт уже выполняется", 429, headers={"Retry-After": "10"})(
                    scope, receive, send
                )
                return
            try:
                await self.app(scope, receive, send)
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(slot.__exit__, None, None, None)
        else:
            await self.app(scope, receive, send)


class BodyLimitExceeded(MultiPartException):
    """MultiPartException ensures Starlette closes already opened upload files."""


class MultipartBudgetExceeded(MultiPartException):
    pass


class BoundedFormParser(FormParser):
    def on_field_name(self, data: bytes, start: int, end: int) -> None:
        self._check_size(end - start)
        super().on_field_name(data, start, end)

    def on_field_data(self, data: bytes, start: int, end: int) -> None:
        self._check_size(end - start)
        super().on_field_data(data, start, end)

    def _check_size(self, count: int) -> None:
        if self._current_field_size + count > self.max_part_size:
            raise MultipartBudgetExceeded("Form field exceeds budget")

    def on_field_end(self) -> None:
        if self._current_fields >= self.max_fields:
            raise MultipartBudgetExceeded("Too many form fields")
        super().on_field_end()


class BoundedMultiPartParser(MultiPartParser):
    """Close spooled files on disconnect/cancellation as well as parser errors.

    The pinned Starlette parser counts text parts but not file bytes. Keep file
    counting in its streaming callbacks, before data is written to the spool.
    """
    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._part_bytes = 0
        self._header_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self._part_bytes += end - start
        limit = 100 * 1024 * 1024 if self._current_part.file is not None else self.max_part_size
        if self._part_bytes > limit:
            raise MultipartBudgetExceeded("Multipart part exceeds budget")
        super().on_part_data(data, start, end)

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._check_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._check_header(end - start)
        super().on_header_value(data, start, end)

    def _check_header(self, count: int) -> None:
        self._header_bytes += count
        if self._header_bytes > 16384:
            raise MultipartBudgetExceeded("Multipart headers exceed budget")

    def on_header_end(self) -> None:
        if len(self._current_part.item_headers) >= 32:
            raise MultipartBudgetExceeded("Too many multipart headers")
        super().on_header_end()

    def on_end(self) -> None:
        self._complete = True
        super().on_end()

    def on_headers_finished(self) -> None:
        try:
            super().on_headers_finished()
        except MultiPartException as exc:
            if self._current_fields > self.max_fields or (self.max_files and self._current_files > self.max_files):
                raise MultipartBudgetExceeded("Too many form parts") from exc
            raise

    async def parse(self) -> Any:
        self._complete = False
        try:
            result = await super().parse()
            if not self._complete:
                raise MultiPartException("Incomplete multipart form")
            return result
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


class SecurityEnvelope:
    """Wrap even ServerErrorMiddleware so every response has security headers."""
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit = IMPORT_BODY_BYTES if path == "/admin/import" else (
            LOGIN_BODY_BYTES if path == "/admin/login" else FORM_BODY_BYTES)
        total = 0
        exceeded = False
        started = False

        async def bounded_receive() -> dict:
            nonlocal total, exceeded
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    exceeded = True
                    raise BodyLimitExceeded("Request body exceeds the allowed budget")
            return message

        async def secure_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if exceeded:
                    message = {"type": "http.response.start", "status": 413, "headers": []}
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer" if path.startswith("/order/") else "strict-origin-when-cross-origin"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                )
                if path == "/admin" or path.startswith(("/admin/", "/order/")) or path == "/checkout":
                    headers["Cache-Control"] = "no-store"
                    headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            elif message["type"] == "http.response.body" and exceeded:
                message = {"type": "http.response.body", "body": b"Request body too large", "more_body": False}
            await send(message)

        raw_length = Headers(scope=scope).get("content-length")
        if raw_length is not None:
            if not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 12:
                await PlainTextResponse("Invalid Content-Length", 400)(scope, receive, secure_send)
                return
            if int(raw_length) > limit:
                await PlainTextResponse("Request body too large", 413)(scope, receive, secure_send)
                return
        try:
            await self.app(scope, bounded_receive, secure_send)
        except BodyLimitExceeded:
            if not started:
                await PlainTextResponse("Request body too large", 413)(scope, receive, secure_send)


class SecureFastAPI(FastAPI):
    def build_middleware_stack(self) -> Any:
        return SecurityEnvelope(super().build_middleware_stack())
