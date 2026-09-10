from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
from contextlib import asynccontextmanager, closing
from functools import partial
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import anyio
from fastapi import FastAPI, HTTPException, Path as ApiPath, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.formparsers import MultiPartException
from python_multipart.exceptions import MultipartParseError, ParseError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from .csv_export import OrdersCsvExport
from .database import DatabaseBusyError, connect, init_db, translate_database_busy
from .import_service import ImportBusyError, ImportLimitError, run_imports
from .orders import CheckoutError, OutboxWorker, create_checkout, normalize_checkout
from .security import (ADMIN_COOKIE, AdminGate, BoundedFormParser, BoundedMultiPartParser, BodyLimitExceeded,
                       MultipartBudgetExceeded, SecureFastAPI, create_admin_session,
                       revoke_sessions, sync_credentials, validate_settings, verify_password)

ORDER_STATUSES = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "assembling": "Собирается",
    "ready": "Готов",
    "completed": "Завершён",
    "cancelled": "Отменён",
}
DELIVERY_METHODS = {
    "pickup": "Самовывоз",
    "courier": "Доставка",
}
PAYMENT_METHODS = {
    "manager": "После подтверждения менеджером",
    "cash": "При получении",
}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
DatabaseId = Annotated[int, ApiPath(ge=1, le=9223372036854775807)]


def _load_local_env(path: Path) -> None:
    """Load a simple KEY=VALUE .env file without overriding real environment variables."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json_images(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed if isinstance(item, str)]
    except (TypeError, json.JSONDecodeError):
        return []


def _product_title(row: Any) -> str:
    parts = [str(row["brand"] or "").strip(), str(row["model"] or "").strip()]
    color = str(row["color"] or "").strip()
    if color:
        parts.append(color)
    return " ".join(part for part in parts if part)


def _product_dict(row: Any) -> dict[str, Any]:
    images = _json_images(row["images_json"])
    return {
        "id": int(row["id"]),
        "barcode": str(row["barcode"]),
        "brand": str(row["brand"]),
        "category": str(row["category"]),
        "model": str(row["model"]),
        "color": str(row["color"]),
        "box_qty": str(row["box_qty"]),
        "price_cents": int(row["price_cents"]),
        "description": str(row["description"]),
        "badge": str(row["badge"]),
        "images": images,
        "image": images[0] if images else "/static/img/placeholder.svg",
        "active": bool(row["active"]),
        "title": _product_title(row),
    }


def _admin_product_dict(row: Any) -> dict[str, Any]:
    return {**_product_dict(row), "source_price_cents": int(row["source_price_cents"]),
            "updated_at": str(row["updated_at"])}


def _money(cents: int | str | None) -> str:
    try:
        amount = int(cents or 0) / 100
    except (TypeError, ValueError):
        amount = 0
    formatted = f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}"
    return formatted.replace(",", " ").replace(".", ",") + " ₽"


def _safe_next_url(value: str | None) -> str:
    if not value or len(value) > 2048 or any(ord(c) < 32 for c in value) or "\\" in value:
        return "/admin"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/admin"
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return "/admin"
    if not re.fullmatch(r"/admin(?:/products|/orders(?:/[1-9][0-9]{0,18})?)?", parsed.path):
        return "/admin"
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def _parse_positive_int(value: Any, *, default: int = 1, maximum: int = 999) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(maximum, parsed))


def _parse_price_to_cents(value: str) -> int:
    if len(value) > 64:
        raise ValueError("Цена вне допустимого диапазона")
    normalized = value.strip().replace(" ", "").replace(",", ".").replace("₽", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Цена должна быть числом") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("100000000"):
        raise ValueError("Цена вне допустимого диапазона")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return str(token)


async def _verified_form(
    request: Request, *, max_files: int = 0, max_fields: int = 20, max_part_size: int = 16384,
) -> Any:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        raise HTTPException(415, "Неподдерживаемый формат формы")
    expected = request.session.get("csrf_token", "")
    if not isinstance(expected, str) or not expected:
        raise HTTPException(400, "Обновите страницу перед отправкой формы")
    header_token = request.headers.get("x-csrf-token")
    if header_token is not None and (len(header_token) > 128 or not hmac.compare_digest(
        expected.encode("utf-8"), header_token.encode("utf-8")
    )):
        raise HTTPException(400, "Некорректный CSRF-токен")
    try:
        if content_type == "multipart/form-data":
            parser = BoundedMultiPartParser(request.headers, request.stream(), max_files=max_files,
                                            max_fields=max_fields, max_part_size=max_part_size)
            form = await parser.parse()
        else:
            form = await BoundedFormParser(request.headers, request.stream(), max_fields=max_fields,
                                           max_part_size=max_part_size).parse()
    except (BodyLimitExceeded, MultipartBudgetExceeded) as exc:
        raise HTTPException(413, "Превышен размер запроса или поля") from exc
    except MultipartParseError as exc:
        # python-multipart 0.0.32 exposes header budgets through this general
        # error class. These exact messages are covered by regression tests.
        if str(exc) in {"Maximum header size exceeded", "Maximum header count exceeded"}:
            raise HTTPException(413, "Превышен лимит заголовков multipart") from exc
        raise HTTPException(400, "Некорректная multipart-форма") from exc
    except (MultiPartException, ParseError) as exc:
        raise HTTPException(400, "Некорректная multipart-форма") from exc
    try:
        fields = form.multi_items()
        if sum(not isinstance(value, StarletteUploadFile) for _, value in fields) > max_fields:
            raise HTTPException(413, "Слишком много полей")
        seen: set[str] = set()
        for key, value in fields:
            if len(key) > 100 or (key in seen and not (max_files and key == "files")):
                raise HTTPException(400, "Повторяющееся или некорректное поле")
            seen.add(key)
            if isinstance(value, StarletteUploadFile):
                if not max_files or key != "files":
                    raise HTTPException(400, "Файлы в этой форме не поддерживаются")
                if (value.size or 0) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Файл превышает лимит 100 MiB")
            elif not isinstance(value, str) or len(value.encode("utf-8")) > max_part_size:
                raise HTTPException(413, "Поле превышает допустимый размер")
        supplied = form.get("csrf_token", "")
        if not isinstance(supplied, str) or len(supplied) > 128 or not hmac.compare_digest(
            expected.encode("utf-8"), supplied.encode("utf-8")
        ):
            raise HTTPException(400, "Некорректный CSRF-токен. Обновите страницу.")
    except BaseException:
        with anyio.CancelScope(shield=True):
            await form.close()
        raise
    if max_files == 0:
        await form.close()
    return form


def _flash(request: Request, message: str, category: str = "info") -> None:
    flashes = list(request.session.get("_flashes", []))
    flashes.append([category, message])
    request.session["_flashes"] = flashes[-20:]


def _pop_flashes(request: Request) -> list[list[str]]:
    flashes = request.session.pop("_flashes", [])
    return flashes if isinstance(flashes, list) else []


def _admin_authenticated(request: Request) -> bool:
    return bool(getattr(request.state, "admin_authenticated", False))


def _admin_redirect(request: Request) -> RedirectResponse:
    next_url = request.url.path if request.method == "GET" else "/admin"
    if request.method == "GET" and request.url.query:
        next_url += "?" + request.url.query
    return RedirectResponse(url=f"/admin/login?next={quote(next_url)}", status_code=303)


def create_app(test_config: dict[str, Any] | None = None) -> FastAPI:
    root = PROJECT_ROOT
    if test_config is None:
        _load_local_env(root / ".env")
    settings: dict[str, Any] = {
        "APP_ENV": os.getenv("APP_ENV", "production"),
        "SECRET_KEY": os.getenv("SECRET_KEY", ""),
        "ADMIN_PASSWORD_HASH": os.getenv("ADMIN_PASSWORD_HASH", ""),
        "ALLOWED_HOSTS": os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1"),
        "DATABASE_PATH": os.getenv("DATABASE_PATH", str(root / "data" / "shop.db")),
        "UPLOAD_ROOT": os.getenv("UPLOAD_ROOT", str(root / "data" / "media")),
        "IMPORT_ARCHIVE": os.getenv("IMPORT_ARCHIVE", str(root / "data" / "imports")),
        "ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", ""),
        "STORE_NAME": os.getenv("STORE_NAME", "ХОКО Каталог"),
        "STORE_PHONE": os.getenv("STORE_PHONE", "+7 999 000-00-00"),
        "STORE_TELEGRAM": os.getenv("STORE_TELEGRAM", ""),
        "LEGAL_OPERATOR_NAME": os.getenv("LEGAL_OPERATOR_NAME", ""),
        "LEGAL_OPERATOR_ADDRESS": os.getenv("LEGAL_OPERATOR_ADDRESS", ""),
        "LEGAL_OPERATOR_EMAIL": os.getenv("LEGAL_OPERATOR_EMAIL", ""),
        "PRIVACY_POLICY_VERSION": os.getenv("PRIVACY_POLICY_VERSION", "2026-09-02"),
        "PRICE_MULTIPLIER": os.getenv("PRICE_MULTIPLIER", "1.0"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
        "SESSION_COOKIE_SECURE": _env_bool("SESSION_COOKIE_SECURE", True),
        **{key: os.getenv(key, default) for key, default in {
            "ADMIN_IDLE_SECONDS": 1800, "ADMIN_ABSOLUTE_SECONDS": 28800,
            "LOGIN_RATE_LIMIT": 20, "LOGIN_RATE_WINDOW": 300,
            "CHECKOUT_RATE_LIMIT": 60, "CHECKOUT_RATE_WINDOW": 600,
            "FORM_BODY_IDLE_SECONDS": 30, "FORM_BODY_TOTAL_SECONDS": 180,
            "IMPORT_BODY_IDLE_SECONDS": 90, "IMPORT_BODY_TOTAL_SECONDS": 1800,
        }.items()},
        "PER_PAGE": 36,
    }
    if test_config:
        settings.update(test_config)
    validate_settings(settings)
    if not str(settings.get("LEGAL_OPERATOR_NAME", "")).strip():
        settings["LEGAL_OPERATOR_NAME"] = settings["STORE_NAME"]

    Path(settings["UPLOAD_ROOT"]).mkdir(parents=True, exist_ok=True)
    Path(settings["IMPORT_ARCHIVE"]).mkdir(parents=True, exist_ok=True)
    init_db(settings["DATABASE_PATH"])

    sync_credentials(settings)
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        worker = OutboxWorker(settings["DATABASE_PATH"], str(settings["TELEGRAM_BOT_TOKEN"]),
                              str(settings["TELEGRAM_CHAT_ID"]))
        worker.start()
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(worker.stop)

    app = SecureFastAPI(title="ХОКО Каталог", docs_url=None, redoc_url=None, openapi_url=None,
                        lifespan=lifespan, security_settings=settings)
    app.state.login_limiter = anyio.CapacityLimiter(2)
    app.state.import_limiter = anyio.CapacityLimiter(1)
    app.add_middleware(AdminGate, settings=settings)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=str(settings["SECRET_KEY"]),
        same_site="lax",
        https_only=bool(settings["SESSION_COOKIE_SECURE"]),
        max_age=60 * 60 * 24 * 14,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings["ALLOWED_HOSTS"], www_redirect=False)
    app.mount("/static", StaticFiles(directory=str(root / "static")), name="static")

    templates = Jinja2Templates(directory=str(root / "templates"))
    templates.env.filters["money"] = _money

    def render(request: Request, name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
        endpoint_obj = request.scope.get("endpoint")
        endpoint = getattr(endpoint_obj, "__name__", "")

        def template_url_for(route_name: str, **params: Any) -> Any:
            path_param_names: set[str] = set()
            for route in request.app.routes:
                if getattr(route, "name", None) == route_name:
                    path_param_names = set(getattr(route, "param_convertors", {}).keys())
                    break
            path_params = {
                key: value for key, value in params.items()
                if key in path_param_names and value is not None
            }
            query_params = {
                key: value for key, value in params.items()
                if key not in path_param_names and value not in (None, "")
            }
            url = request.url_for(route_name, **path_params)
            return url.include_query_params(**query_params) if query_params else url

        base_context = {
            "request": request,
            "url_for": template_url_for,
            "session": request.session,
            "admin_authenticated": _admin_authenticated(request),
            "flashes": _pop_flashes(request),
            "csrf_token": lambda: _ensure_csrf(request),
            "store_name": settings["STORE_NAME"],
            "store_phone": settings["STORE_PHONE"],
            "store_telegram": settings["STORE_TELEGRAM"],
            "legal_operator_name": settings["LEGAL_OPERATOR_NAME"],
            "legal_operator_address": settings["LEGAL_OPERATOR_ADDRESS"],
            "legal_operator_email": settings["LEGAL_OPERATOR_EMAIL"],
            "privacy_policy_version": settings["PRIVACY_POLICY_VERSION"],
            "order_statuses": ORDER_STATUSES,
            "delivery_methods": DELIVERY_METHODS,
            "payment_methods": PAYMENT_METHODS,
            "endpoint": endpoint,
        }
        base_context.update(context)
        return templates.TemplateResponse(request=request, name=name, context=base_context, status_code=status_code)

    @app.get("/", response_class=HTMLResponse, name="catalog")
    def catalog(request: Request) -> HTMLResponse:
        query = request.query_params.get("q", "").strip()[:100]
        brand = request.query_params.get("brand", "").strip()[:80]
        category = request.query_params.get("category", "").strip()[:120]
        sort = request.query_params.get("sort", "popular")
        page = _parse_positive_int(request.query_params.get("page"), default=1, maximum=100000)
        per_page = int(settings["PER_PAGE"])

        where = ["active = 1"]
        params: list[Any] = []
        if query:
            where.append(
                "(model LIKE ? COLLATE NOCASE OR barcode LIKE ? OR brand LIKE ? COLLATE NOCASE "
                "OR category LIKE ? COLLATE NOCASE OR description LIKE ? COLLATE NOCASE)"
            )
            needle = f"%{query}%"
            params.extend([needle, needle, needle, needle, needle])
        if brand:
            where.append("brand = ?")
            params.append(brand)
        if category:
            where.append("category = ?")
            params.append(category)
        order_by = {
            "price_asc": "price_cents ASC, model ASC",
            "price_desc": "price_cents DESC, model ASC",
            "name": "model COLLATE NOCASE ASC",
            "new": "CASE WHEN badge <> '' THEN 0 ELSE 1 END, updated_at DESC",
            "popular": "CASE WHEN badge <> '' THEN 0 ELSE 1 END, brand ASC, category ASC, model ASC",
        }.get(sort, "CASE WHEN badge <> '' THEN 0 ELSE 1 END, brand ASC, category ASC, model ASC")
        where_sql = " AND ".join(where)
        # Clauses and ordering come exclusively from constants above; values use bound parameters.
        with closing(connect(settings["DATABASE_PATH"])) as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params).fetchone()[0])  # nosec B608
            pages = max(1, math.ceil(total / per_page))
            page = min(page, pages)
            rows = db.execute(
                f"SELECT * FROM products WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",  # nosec B608
                [*params, per_page, (page - 1) * per_page],
            ).fetchall()
            products = [_product_dict(row) for row in rows]
            brands = db.execute(
                "SELECT brand, COUNT(*) AS count FROM products WHERE active = 1 GROUP BY brand ORDER BY brand"
            ).fetchall()
            categories = db.execute(
                "SELECT category, COUNT(*) AS count FROM products WHERE active = 1 AND category <> '' "
                "GROUP BY category ORDER BY count DESC, category LIMIT 80"
            ).fetchall()
            product_count = int(db.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0])
        return render(
            request,
            "catalog.html",
            products=products,
            brands=brands,
            categories=categories,
            product_count=product_count,
            total=total,
            query=query,
            selected_brand=brand,
            selected_category=category,
            sort=sort,
            page=page,
            pages=pages,
        )

    @app.get("/product/{product_id}", response_class=HTMLResponse, name="product_detail")
    def product_detail(request: Request, product_id: DatabaseId) -> HTMLResponse:
        with closing(connect(settings["DATABASE_PATH"])) as db:
            row = db.execute("SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Товар не найден")
            product = _product_dict(row)
            related_rows = db.execute(
                """
                SELECT * FROM products
                WHERE active = 1 AND category = ? AND id <> ?
                ORDER BY CASE WHEN brand = ? THEN 0 ELSE 1 END, model
                LIMIT 8
                """,
                (product["category"], product_id, product["brand"]),
            ).fetchall()
        return render(request, "product.html", product=product, related=[_product_dict(item) for item in related_rows])

    @app.get("/api/products", response_class=JSONResponse, name="api_products")
    def api_products(request: Request) -> JSONResponse:
        raw_ids = request.query_params.get("ids", "")
        if len(raw_ids) > 2100:
            raise HTTPException(400, "Слишком длинный список ID")
        ids: list[int] = []
        if raw_ids:
            raw_values = raw_ids.split(",")
            if len(raw_values) > 100:
                raise HTTPException(400, "Слишком много ID")
            for raw in raw_values:
                if not re.fullmatch(r"[1-9][0-9]{0,18}", raw) or int(raw) > 9223372036854775807:
                    raise HTTPException(400, "Некорректный ID")
                value = int(raw)
                if value not in ids:
                    ids.append(value)
        if not ids:
            return JSONResponse({"products": []})
        placeholders = ",".join("?" for _ in ids)
        # Only the number of placeholders is interpolated, never an ID or other input.
        with closing(connect(settings["DATABASE_PATH"])) as db:
            rows = db.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders}) AND active = 1", ids  # nosec B608
            ).fetchall()
        by_id = {int(row["id"]): _product_dict(row) for row in rows}
        return JSONResponse({"products": [by_id[item_id] for item_id in ids if item_id in by_id]})

    @app.get("/privacy", response_class=HTMLResponse, name="privacy_policy")
    def privacy_policy(request: Request) -> HTMLResponse:
        return render(request, "privacy.html")

    @app.get("/checkout", response_class=HTMLResponse, name="checkout")
    def checkout(request: Request) -> HTMLResponse:
        request.session.setdefault("checkout_scope", secrets.token_urlsafe(32))
        return render(request, "checkout.html", idempotency_key=secrets.token_urlsafe(32))

    @app.post("/checkout", name="create_order")
    async def create_order(request: Request) -> RedirectResponse:
        form = await _verified_form(request)
        scope = request.session.get("checkout_scope")
        if not isinstance(scope, str) or not scope:
            raise HTTPException(400, "Откройте страницу оформления заказа")
        try:
            payload = normalize_checkout(form)
            result = await anyio.to_thread.run_sync(partial(
                create_checkout, settings["DATABASE_PATH"], scope, str(form.get("idempotency_key", "")),
                payload, str(settings["PRIVACY_POLICY_VERSION"]),
                notify=bool(settings["TELEGRAM_BOT_TOKEN"] and settings["TELEGRAM_CHAT_ID"]),
            ))
        except CheckoutError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return RedirectResponse(url=f"/order/{result.public_token}", status_code=303)

    @app.get("/order/{token}", response_class=HTMLResponse, name="order_success")
    def order_success(request: Request, token: str) -> HTMLResponse:
        with closing(connect(settings["DATABASE_PATH"])) as db:
            order = db.execute("SELECT * FROM orders WHERE public_token = ?", (token,)).fetchone()
            if order is None:
                raise HTTPException(status_code=404, detail="Заказ не найден")
            items = db.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order["id"],)).fetchall()
        return render(request, "order_success.html", order=order, items=items, clear_cart=True)

    @app.get("/media/{filename:path}", name="media")
    def media(filename: str) -> FileResponse:
        if (len(filename) > 1024 or any(ord(char) < 32 for char in filename)
                or any(part.startswith(".") for part in filename.replace("\\", "/").split("/"))
                or Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}):
            raise HTTPException(status_code=404)
        root_path = Path(settings["UPLOAD_ROOT"]).resolve()
        try:
            file_path = (root_path / filename).resolve()
            exists = file_path.is_file()
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404) from exc
        if root_path != file_path and root_path not in file_path.parents:
            raise HTTPException(status_code=404)
        if not exists:
            raise HTTPException(status_code=404)
        return FileResponse(file_path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/healthz", response_class=JSONResponse, name="health")
    def health() -> JSONResponse:
        with closing(connect(settings["DATABASE_PATH"])) as db:
            db.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok"})

    @app.get("/admin/login", response_class=HTMLResponse, name="admin_login")
    def admin_login_get(request: Request) -> HTMLResponse:
        return render(request, "admin/login.html", next_url=_safe_next_url(request.query_params.get("next")))

    @app.post("/admin/login", name="admin_login_post")
    async def admin_login_post(request: Request) -> RedirectResponse:
        form = await _verified_form(request)
        password = str(form.get("password", ""))
        valid = await anyio.to_thread.run_sync(
            verify_password, str(settings["ADMIN_PASSWORD_HASH"]), password, limiter=app.state.login_limiter
        )
        if valid:
            old_cookie = request.cookies.get(ADMIN_COOKIE, "")
            await anyio.to_thread.run_sync(revoke_sessions, settings, old_cookie)
            token = await anyio.to_thread.run_sync(create_admin_session, settings)
            # Preserve guest checkout scope, rotate its CSRF on privilege change.
            request.session.pop("admin_authenticated", None)
            request.session["csrf_token"] = secrets.token_urlsafe(32)
            response = RedirectResponse(url=_safe_next_url(str(form.get("next", "/admin"))), status_code=303)
            response.set_cookie(ADMIN_COOKIE, token, max_age=settings["ADMIN_ABSOLUTE_SECONDS"],
                                httponly=True, secure=settings["SESSION_COOKIE_SECURE"], samesite="strict", path="/")
            return response
        next_url = _safe_next_url(str(form.get("next", "/admin")))
        _flash(request, "Неверный пароль", "error")
        return RedirectResponse(url=f"/admin/login?next={quote(next_url)}", status_code=303)

    @app.post("/admin/logout", name="admin_logout")
    async def admin_logout(request: Request) -> RedirectResponse:
        await _verified_form(request)
        await anyio.to_thread.run_sync(revoke_sessions, settings, request.cookies.get(ADMIN_COOKIE, ""))
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/", secure=settings["SESSION_COOKIE_SECURE"],
                               httponly=True, samesite="strict")
        return response

    @app.post("/admin/sessions/revoke", name="admin_revoke_sessions")
    async def admin_revoke_sessions(request: Request) -> RedirectResponse:
        await _verified_form(request)
        await anyio.to_thread.run_sync(revoke_sessions, settings)
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        response = RedirectResponse(url="/admin/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/", secure=settings["SESSION_COOKIE_SECURE"],
                               httponly=True, samesite="strict")
        return response

    @app.get("/admin", response_class=HTMLResponse, name="admin_dashboard")
    def admin_dashboard(request: Request) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            stats = {
                "active_products": int(db.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]),
                "inactive_products": int(db.execute("SELECT COUNT(*) FROM products WHERE active = 0").fetchone()[0]),
                "new_orders": int(db.execute("SELECT COUNT(*) FROM orders WHERE status = 'new'").fetchone()[0]),
                "orders_total": int(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
                "revenue_cents": int(db.execute("SELECT COALESCE(SUM(total_cents), 0) FROM orders WHERE status <> 'cancelled'").fetchone()[0]),
            }
            recent_orders = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
            import_runs = db.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 10").fetchall()
        return render(
            request,
            "admin/dashboard.html",
            stats=stats,
            recent_orders=recent_orders,
            import_runs=import_runs,
            price_multiplier=settings["PRICE_MULTIPLIER"],
        )

    @app.post("/admin/import", name="admin_import")
    async def admin_import(request: Request) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        form = await _verified_form(request, max_files=10, max_fields=10)
        try:
            files = [file for file in form.getlist("files")
                     if isinstance(file, StarletteUploadFile) and file.filename]
            deactivate_missing = str(form.get("deactivate_missing", "")) == "1"
            multiplier_raw = str(form.get("price_multiplier", settings["PRICE_MULTIPLIER"])).strip()
            try:
                if len(multiplier_raw) > 32:
                    raise InvalidOperation
                multiplier = Decimal(multiplier_raw)
                if not multiplier.is_finite() or multiplier <= 0 or multiplier > Decimal("100"):
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise HTTPException(400, "Некорректный коэффициент цены") from exc
            if not files or len(files) > 10 or (deactivate_missing and len(files) != 1):
                raise HTTPException(400, "Выберите до 10 XLSX; скрытие отсутствующих требует одного файла")
            try:
                completed, failed = await anyio.to_thread.run_sync(partial(
                    run_imports, settings["DATABASE_PATH"], settings["UPLOAD_ROOT"], settings["IMPORT_ARCHIVE"],
                    [(file.filename or "price.xlsx", file.file) for file in files],
                    price_multiplier=multiplier, deactivate_missing=deactivate_missing,
                ), limiter=app.state.import_limiter)
            except ImportLimitError as exc:
                raise HTTPException(413, "Превышен бюджет импорта") from exc
            except ImportBusyError as exc:
                raise HTTPException(429, "Импорт уже выполняется", headers={"Retry-After": "10"}) from exc
            except ValueError as exc:
                raise HTTPException(400, "Некорректный XLSX-файл") from exc
        finally:
            with anyio.CancelScope(shield=True):
                await form.close()
        if completed:
            settings["PRICE_MULTIPLIER"] = str(multiplier)

        for result in completed:
            _flash(
                request,
                f"{result.filename}: добавлено {result.inserted}, обновлено {result.updated}, "
                f"без изменений {result.unchanged}, отключено {result.deactivated}, фото {result.image_count}",
                "success",
            )
            if result.warnings:
                _flash(request, f"{result.filename}: предупреждений {len(result.warnings)}", "warning")
        for error in failed:
            _flash(request, error, "error")
        return RedirectResponse(url="/admin", status_code=303)

    @app.get("/admin/orders", response_class=HTMLResponse, name="admin_orders")
    def admin_orders(request: Request) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        status = request.query_params.get("status", "").strip()
        query = request.query_params.get("q", "").strip()[:100]
        where = ["1 = 1"]
        params: list[Any] = []
        if status in ORDER_STATUSES:
            where.append("status = ?")
            params.append(status)
        if query:
            needle = f"%{query}%"
            where.append("(order_number LIKE ? OR customer_name LIKE ? COLLATE NOCASE OR phone LIKE ?)")
            params.extend([needle, needle, needle])
        with closing(connect(settings["DATABASE_PATH"])) as db:
            # The clauses are constant strings; search/status values remain bound parameters.
            orders = db.execute(
                f"SELECT * FROM orders WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 300", params  # nosec B608
            ).fetchall()
        return render(request, "admin/orders.html", orders=orders, selected_status=status, query=query)

    @app.get("/admin/orders/{order_id}", response_class=HTMLResponse, name="admin_order_detail")
    def admin_order_detail(request: Request, order_id: DatabaseId) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise HTTPException(status_code=404, detail="Заказ не найден")
            items = db.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
        return render(request, "admin/order_detail.html", order=order, items=items)

    @app.post("/admin/orders/{order_id}", name="admin_order_update")
    async def admin_order_update(request: Request, order_id: DatabaseId) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        form = await _verified_form(request)
        status = str(form.get("status", ""))
        if status not in ORDER_STATUSES:
            _flash(request, "Неизвестный статус", "error")
            return RedirectResponse(url=f"/admin/orders/{order_id}", status_code=303)
        def update_order() -> None:
            with translate_database_busy(), closing(connect(settings["DATABASE_PATH"])) as db, db:
                cursor = db.execute(
                    "UPDATE orders SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                    (status, order_id),
                )
                if cursor.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Заказ не найден")
        await anyio.to_thread.run_sync(update_order)
        _flash(request, "Статус заказа обновлён", "success")
        return RedirectResponse(url=f"/admin/orders/{order_id}", status_code=303)

    @app.get("/admin/products", response_class=HTMLResponse, name="admin_products")
    def admin_products(request: Request) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        query = request.query_params.get("q", "").strip()[:100]
        active_filter = request.query_params.get("active", "all")
        page = _parse_positive_int(request.query_params.get("page"), default=1, maximum=100000)
        per_page = 80
        where = ["1 = 1"]
        params: list[Any] = []
        if query:
            needle = f"%{query}%"
            where.append("(model LIKE ? COLLATE NOCASE OR barcode LIKE ? OR brand LIKE ? COLLATE NOCASE)")
            params.extend([needle, needle, needle])
        if active_filter == "1":
            where.append("active = 1")
        elif active_filter == "0":
            where.append("active = 0")
        where_sql = " AND ".join(where)
        # Only constant clauses are composed; all search values use bound parameters.
        with closing(connect(settings["DATABASE_PATH"])) as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params).fetchone()[0])  # nosec B608
            pages = max(1, math.ceil(total / per_page))
            page = min(page, pages)
            rows = db.execute(
                f"SELECT * FROM products WHERE {where_sql} ORDER BY brand, category, model LIMIT ? OFFSET ?",  # nosec B608
                [*params, per_page, (page - 1) * per_page],
            ).fetchall()
        return render(
            request,
            "admin/products.html",
            products=[_admin_product_dict(row) for row in rows],
            query=query,
            active_filter=active_filter,
            page=page,
            pages=pages,
            total=total,
        )

    @app.post("/admin/products/{product_id}", name="admin_product_update")
    async def admin_product_update(request: Request, product_id: DatabaseId) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        form = await _verified_form(request)
        try:
            price_cents = _parse_price_to_cents(str(form.get("price", "")))
        except ValueError as exc:
            _flash(request, str(exc), "error")
            return RedirectResponse(url="/admin/products", status_code=303)
        active = 1 if str(form.get("active", "")) == "1" else 0
        def update_product() -> str:
            with translate_database_busy(), closing(connect(settings["DATABASE_PATH"])) as db, db:
                row = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Товар не найден")
                db.execute(
                    "UPDATE products SET price_cents=?, active=?, "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (price_cents, active, product_id),
                )
                return _product_title(row)
        title = await anyio.to_thread.run_sync(update_product)
        _flash(request, f"{title}: сохранено", "success")
        return RedirectResponse(url="/admin/products", status_code=303)

    @app.get("/admin/export/orders.csv", name="admin_orders_csv")
    async def admin_orders_csv(request: Request) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        export = OrdersCsvExport(
            settings["DATABASE_PATH"], ORDER_STATUSES, DELIVERY_METHODS, PAYMENT_METHODS,
        )
        first = await export.start()
        return StreamingResponse(
            export.body(first),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=orders.csv"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        message = str(exc.detail) if exc.detail else ("Страница не найдена" if exc.status_code == 404 else "Некорректный запрос")
        response = render(request, "error.html", status_code=exc.status_code, code=exc.status_code, message=message)
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(DatabaseBusyError)
    async def database_busy_handler(request: Request, _exc: DatabaseBusyError) -> Response:
        headers = {"Retry-After": "1", "Cache-Control": "no-store"}
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Сервис временно занят. Повторите запрос."},
                                status_code=503, headers=headers)
        return PlainTextResponse("Сервис временно занят. Повторите запрос.", status_code=503, headers=headers)

    return app
