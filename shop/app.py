from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from .database import connect, init_db
from .telegram import send_order_notification
from .xlsx_importer import ImportResult, import_xlsx

LOGGER = logging.getLogger(__name__)
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
_load_local_env(PROJECT_ROOT / ".env")


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
        "source_price_cents": int(row["source_price_cents"]),
        "description": str(row["description"]),
        "badge": str(row["badge"]),
        "images": images,
        "image": images[0] if images else "/static/img/placeholder.svg",
        "active": bool(row["active"]),
        "title": _product_title(row),
        "updated_at": str(row["updated_at"]),
    }


def _money(cents: int | str | None) -> str:
    try:
        amount = int(cents or 0) / 100
    except (TypeError, ValueError):
        amount = 0
    formatted = f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}"
    return formatted.replace(",", " ").replace(".", ",") + " ₽"


def _safe_next_url(value: str | None) -> str:
    if not value:
        return "/admin"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/admin"):
        return "/admin"
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def _parse_positive_int(value: Any, *, default: int = 1, maximum: int = 999) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, parsed))


def _parse_price_to_cents(value: str) -> int:
    normalized = value.strip().replace(" ", "").replace(",", ".").replace("₽", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Цена должна быть числом") from exc
    if amount < 0 or amount > Decimal("100000000"):
        raise ValueError("Цена вне допустимого диапазона")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _safe_filename(value: str) -> str:
    name = Path(value).name
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "price"
    return f"{stem[:80]}{suffix if suffix else '.xlsx'}"


def _csv_cell(value: Any) -> str:
    """Prevent spreadsheet formula execution when exported CSV is opened."""
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return str(token)


async def _verified_form(
    request: Request,
    *,
    max_files: int = 10,
    max_fields: int = 200,
    max_part_size: int = MAX_UPLOAD_BYTES,
) -> Any:
    form = await request.form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)
    expected = str(request.session.get("csrf_token", ""))
    supplied = str(form.get("csrf_token", ""))
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=400, detail="Некорректный CSRF-токен. Обновите страницу и повторите действие.")
    return form


def _flash(request: Request, message: str, category: str = "info") -> None:
    flashes = list(request.session.get("_flashes", []))
    flashes.append([category, message])
    request.session["_flashes"] = flashes[-20:]


def _pop_flashes(request: Request) -> list[list[str]]:
    flashes = request.session.pop("_flashes", [])
    return flashes if isinstance(flashes, list) else []


def _admin_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))


def _admin_redirect(request: Request) -> RedirectResponse:
    next_url = request.url.path if request.method == "GET" else "/admin"
    if request.method == "GET" and request.url.query:
        next_url += "?" + request.url.query
    return RedirectResponse(url=f"/admin/login?next={quote(next_url)}", status_code=303)


def create_app(test_config: dict[str, Any] | None = None) -> FastAPI:
    root = PROJECT_ROOT
    settings: dict[str, Any] = {
        "SECRET_KEY": os.getenv("SECRET_KEY", "dev-secret-change-before-public-launch"),
        "DATABASE_PATH": os.getenv("DATABASE_PATH", str(root / "data" / "shop.db")),
        "UPLOAD_ROOT": os.getenv("UPLOAD_ROOT", str(root / "data" / "media")),
        "IMPORT_ARCHIVE": os.getenv("IMPORT_ARCHIVE", str(root / "data" / "imports")),
        "ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", "change-me-now"),
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
        "SESSION_COOKIE_SECURE": _env_bool("SESSION_COOKIE_SECURE", False),
        "PER_PAGE": 36,
    }
    if test_config:
        settings.update(test_config)
    if not str(settings.get("LEGAL_OPERATOR_NAME", "")).strip():
        settings["LEGAL_OPERATOR_NAME"] = settings["STORE_NAME"]

    Path(settings["UPLOAD_ROOT"]).mkdir(parents=True, exist_ok=True)
    Path(settings["IMPORT_ARCHIVE"]).mkdir(parents=True, exist_ok=True)
    init_db(settings["DATABASE_PATH"])

    app = FastAPI(title="ХОКО Каталог", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=str(settings["SECRET_KEY"]),
        same_site="lax",
        https_only=bool(settings["SESSION_COOKIE_SECURE"]),
        max_age=60 * 60 * 24 * 14,
    )
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

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_UPLOAD_BYTES:
                    response = HTMLResponse(
                        "<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>Файл слишком большой</title>"
                        "<body><main><h1>Файл слишком большой</h1>"
                        "<p>Лимит загрузки — 100 МБ.</p><a href=\"/admin\">Вернуться в админку</a>"
                        "</main></body></html>",
                        status_code=413,
                    )
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["X-Frame-Options"] = "DENY"
                    return response
            except ValueError:
                pass
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return response

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
        with closing(connect(settings["DATABASE_PATH"])) as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params).fetchone()[0])
            pages = max(1, math.ceil(total / per_page))
            page = min(page, pages)
            rows = db.execute(
                f"SELECT * FROM products WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
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
    def product_detail(request: Request, product_id: int) -> HTMLResponse:
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
        ids: list[int] = []
        for raw in raw_ids.split(",")[:100]:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        if not ids:
            return JSONResponse({"products": []})
        placeholders = ",".join("?" for _ in ids)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            rows = db.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders}) AND active = 1", ids
            ).fetchall()
        by_id = {int(row["id"]): _product_dict(row) for row in rows}
        return JSONResponse({"products": [by_id[item_id] for item_id in ids if item_id in by_id]})

    @app.get("/privacy", response_class=HTMLResponse, name="privacy_policy")
    def privacy_policy(request: Request) -> HTMLResponse:
        return render(request, "privacy.html")

    @app.get("/checkout", response_class=HTMLResponse, name="checkout")
    def checkout(request: Request) -> HTMLResponse:
        return render(request, "checkout.html")

    @app.post("/checkout", name="create_order")
    async def create_order(request: Request) -> RedirectResponse:
        form = await _verified_form(request)
        customer_name = str(form.get("customer_name", "")).strip()[:100]
        phone = str(form.get("phone", "")).strip()[:40]
        telegram = str(form.get("telegram", "")).strip()[:80]
        delivery_method = str(form.get("delivery_method", "pickup"))
        payment_method = str(form.get("payment_method", "manager"))
        address = str(form.get("address", "")).strip()[:500]
        comment = str(form.get("comment", "")).strip()[:1000]
        personal_data_consent = str(form.get("personal_data_consent", "")).strip().lower() in {"1", "true", "yes", "on"}
        cart_raw = str(form.get("cart_json", ""))

        errors: list[str] = []
        if len(customer_name) < 2:
            errors.append("Укажите имя")
        if len(re.sub(r"\D", "", phone)) < 7:
            errors.append("Укажите корректный телефон")
        if delivery_method not in DELIVERY_METHODS:
            errors.append("Некорректный способ получения")
        if payment_method not in PAYMENT_METHODS:
            errors.append("Некорректный способ оплаты")
        if delivery_method == "courier" and len(address) < 5:
            errors.append("Для доставки нужен адрес")
        if not personal_data_consent:
            errors.append("Для оформления заказа необходимо согласие на обработку персональных данных")
        try:
            cart_payload = json.loads(cart_raw)
        except json.JSONDecodeError:
            cart_payload = []
        requested: list[tuple[int, int]] = []
        seen_ids: set[int] = set()
        if isinstance(cart_payload, list):
            for item in cart_payload[:100]:
                if not isinstance(item, dict):
                    continue
                try:
                    product_id = int(item.get("id"))
                    quantity = int(item.get("quantity"))
                except (TypeError, ValueError):
                    continue
                if product_id <= 0 or product_id in seen_ids or not 1 <= quantity <= 999:
                    continue
                seen_ids.add(product_id)
                requested.append((product_id, quantity))
        if not requested:
            errors.append("Корзина пуста")
        if len(requested) > 50:
            errors.append("В одном заказе допускается не больше 50 разных товаров")
        if errors:
            for error in errors:
                _flash(request, error, "error")
            return RedirectResponse(url="/checkout", status_code=303)

        ids = [product_id for product_id, _quantity in requested]
        placeholders = ",".join("?" for _ in ids)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            rows = db.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders}) AND active = 1", ids
            ).fetchall()
            products_by_id = {int(row["id"]): row for row in rows}
            items: list[dict[str, Any]] = []
            total_cents = 0
            for product_id, quantity in requested:
                row = products_by_id.get(product_id)
                if row is None:
                    continue
                unit_price = int(row["price_cents"])
                subtotal = unit_price * quantity
                total_cents += subtotal
                items.append(
                    {
                        "product_id": product_id,
                        "barcode": str(row["barcode"]),
                        "title": _product_title(row),
                        "unit_price_cents": unit_price,
                        "quantity": quantity,
                        "subtotal_cents": subtotal,
                    }
                )
            if not items:
                _flash(request, "Товары из корзины больше недоступны. Обновите каталог.", "error")
                return RedirectResponse(url="/checkout", status_code=303)
            if len(items) != len(requested):
                _flash(
                    request,
                    "Один или несколько товаров стали недоступны. Корзина обновлена — проверьте её ещё раз.",
                    "error",
                )
                return RedirectResponse(url="/checkout", status_code=303)

            public_token = secrets.token_urlsafe(24)
            consent_at = datetime.now(timezone.utc).isoformat()
            try:
                cursor = db.execute(
                    """
                    INSERT INTO orders(
                        public_token, customer_name, phone, telegram, delivery_method,
                        address, payment_method, comment, personal_data_consent,
                        privacy_policy_version, personal_data_consent_at, status, total_cents
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'new', ?)
                    """,
                    (
                        public_token,
                        customer_name,
                        phone,
                        telegram,
                        delivery_method,
                        address,
                        payment_method,
                        comment,
                        str(settings["PRIVACY_POLICY_VERSION"]),
                        consent_at,
                        total_cents,
                    ),
                )
                order_id = int(cursor.lastrowid)
                date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
                order_number = f"XK-{date_part}-{order_id:05d}"
                db.execute("UPDATE orders SET order_number = ? WHERE id = ?", (order_number, order_id))
                db.executemany(
                    """
                    INSERT INTO order_items(
                        order_id, product_id, barcode, title, unit_price_cents, quantity, subtotal_cents
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            order_id,
                            item["product_id"],
                            item["barcode"],
                            item["title"],
                            item["unit_price_cents"],
                            item["quantity"],
                            item["subtotal_cents"],
                        )
                        for item in items
                    ],
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

        try:
            send_order_notification(
                str(settings["TELEGRAM_BOT_TOKEN"]),
                str(settings["TELEGRAM_CHAT_ID"]),
                {
                    "order_number": order_number,
                    "customer_name": customer_name,
                    "phone": phone,
                    "telegram": telegram,
                    "delivery_method": DELIVERY_METHODS[delivery_method],
                    "address": address,
                    "comment": comment,
                    "total_cents": total_cents,
                },
                items,
            )
        except Exception as exc:
            LOGGER.warning("Telegram order notification failed (%s)", type(exc).__name__)
        return RedirectResponse(url=f"/order/{public_token}", status_code=303)

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
        root_path = Path(settings["UPLOAD_ROOT"]).resolve()
        file_path = (root_path / filename).resolve()
        if root_path != file_path and root_path not in file_path.parents:
            raise HTTPException(status_code=404)
        if not file_path.is_file():
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
        expected = str(settings["ADMIN_PASSWORD"])
        if expected and hmac.compare_digest(password, expected):
            request.session.clear()
            request.session["admin_authenticated"] = True
            _ensure_csrf(request)
            return RedirectResponse(url=_safe_next_url(str(form.get("next", "/admin"))), status_code=303)
        next_url = _safe_next_url(str(form.get("next", "/admin")))
        _flash(request, "Неверный пароль", "error")
        return RedirectResponse(url=f"/admin/login?next={quote(next_url)}", status_code=303)

    @app.post("/admin/logout", name="admin_logout")
    async def admin_logout(request: Request) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        await _verified_form(request)
        request.session.clear()
        return RedirectResponse(url="/", status_code=303)

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
        form = await _verified_form(request, max_files=10, max_fields=50, max_part_size=MAX_UPLOAD_BYTES)
        raw_files = form.getlist("files")
        files = [file for file in raw_files if isinstance(file, StarletteUploadFile) and file.filename]
        deactivate_missing = str(form.get("deactivate_missing", "")) == "1"
        multiplier_raw = str(form.get("price_multiplier", settings["PRICE_MULTIPLIER"])).strip()
        try:
            multiplier = Decimal(multiplier_raw)
            if multiplier <= 0 or multiplier > Decimal("100"):
                raise InvalidOperation
        except InvalidOperation:
            _flash(request, "Некорректный коэффициент цены", "error")
            return RedirectResponse(url="/admin", status_code=303)
        if not files:
            _flash(request, "Выберите хотя бы один XLSX-файл", "error")
            return RedirectResponse(url="/admin", status_code=303)
        if len(files) > 10:
            _flash(request, "За один раз можно загрузить не больше 10 файлов", "error")
            return RedirectResponse(url="/admin", status_code=303)
        if deactivate_missing and len(files) > 1:
            _flash(
                request,
                "Скрытие отсутствующих безопасно выполнять только с одним полным прайсом. "
                "Загрузите файлы без этой галочки или импортируйте полный файл отдельно.",
                "error",
            )
            return RedirectResponse(url="/admin", status_code=303)

        settings["PRICE_MULTIPLIER"] = str(multiplier)
        completed: list[ImportResult] = []
        failed: list[str] = []
        with closing(connect(settings["DATABASE_PATH"])) as db:
            for uploaded in files:
                original_name = Path(uploaded.filename or "price.xlsx").name
                if not original_name.lower().endswith(".xlsx"):
                    failed.append(f"{original_name}: нужен файл .xlsx")
                    continue
                safe_name = _safe_filename(original_name)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                archive_name = f"{stamp}-{secrets.token_hex(4)}-{safe_name}"
                archive_path = Path(settings["IMPORT_ARCHIVE"]) / archive_name
                total_written = 0
                try:
                    with archive_path.open("wb") as destination:
                        while True:
                            chunk = await uploaded.read(1024 * 1024)
                            if not chunk:
                                break
                            total_written += len(chunk)
                            if total_written > MAX_UPLOAD_BYTES:
                                raise ValueError("файл превышает лимит 100 МБ")
                            destination.write(chunk)
                    result = import_xlsx(
                        db,
                        archive_path,
                        settings["UPLOAD_ROOT"],
                        original_filename=original_name,
                        price_multiplier=multiplier,
                        deactivate_missing=deactivate_missing,
                    )
                    completed.append(result)
                except Exception as exc:
                    LOGGER.exception("Import failed for %s", original_name)
                    failed.append(f"{original_name}: {exc}")
                finally:
                    await uploaded.close()

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
            orders = db.execute(
                f"SELECT * FROM orders WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 300", params
            ).fetchall()
        return render(request, "admin/orders.html", orders=orders, selected_status=status, query=query)

    @app.get("/admin/orders/{order_id}", response_class=HTMLResponse, name="admin_order_detail")
    def admin_order_detail(request: Request, order_id: int) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise HTTPException(status_code=404, detail="Заказ не найден")
            items = db.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
        return render(request, "admin/order_detail.html", order=order, items=items)

    @app.post("/admin/orders/{order_id}", name="admin_order_update")
    async def admin_order_update(request: Request, order_id: int) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        form = await _verified_form(request)
        status = str(form.get("status", ""))
        if status not in ORDER_STATUSES:
            _flash(request, "Неизвестный статус", "error")
            return RedirectResponse(url=f"/admin/orders/{order_id}", status_code=303)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            cursor = db.execute(
                "UPDATE orders SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, order_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Заказ не найден")
            db.commit()
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
        with closing(connect(settings["DATABASE_PATH"])) as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params).fetchone()[0])
            pages = max(1, math.ceil(total / per_page))
            page = min(page, pages)
            rows = db.execute(
                f"SELECT * FROM products WHERE {where_sql} ORDER BY brand, category, model LIMIT ? OFFSET ?",
                [*params, per_page, (page - 1) * per_page],
            ).fetchall()
        return render(
            request,
            "admin/products.html",
            products=[_product_dict(row) for row in rows],
            query=query,
            active_filter=active_filter,
            page=page,
            pages=pages,
            total=total,
        )

    @app.post("/admin/products/{product_id}", name="admin_product_update")
    async def admin_product_update(request: Request, product_id: int) -> RedirectResponse:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        form = await _verified_form(request)
        try:
            price_cents = _parse_price_to_cents(str(form.get("price", "")))
        except ValueError as exc:
            _flash(request, str(exc), "error")
            return RedirectResponse(url="/admin/products", status_code=303)
        active = 1 if str(form.get("active", "")) == "1" else 0
        with closing(connect(settings["DATABASE_PATH"])) as db:
            row = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Товар не найден")
            db.execute(
                """
                UPDATE products
                SET price_cents = ?, active = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (price_cents, active, product_id),
            )
            db.commit()
        _flash(request, f"{_product_title(row)}: сохранено", "success")
        return RedirectResponse(url="/admin/products", status_code=303)

    @app.get("/admin/export/orders.csv", name="admin_orders_csv")
    def admin_orders_csv(request: Request) -> Response:
        if not _admin_authenticated(request):
            return _admin_redirect(request)
        with closing(connect(settings["DATABASE_PATH"])) as db:
            rows = db.execute(
                """
                SELECT o.order_number, o.created_at, o.status, o.customer_name, o.phone, o.telegram,
                       o.delivery_method, o.address, o.payment_method, o.total_cents,
                       o.personal_data_consent, o.privacy_policy_version, o.personal_data_consent_at,
                       i.barcode, i.title, i.unit_price_cents, i.quantity, i.subtotal_cents
                FROM orders o
                JOIN order_items i ON i.order_id = o.id
                ORDER BY o.id DESC, i.id
                """
            ).fetchall()
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([
            "Заказ", "Дата", "Статус", "Клиент", "Телефон", "Telegram",
            "Получение", "Адрес", "Оплата", "Итого заказа", "Согласие ПД",
            "Версия политики", "Дата согласия", "Штрихкод", "Товар", "Цена",
            "Количество", "Сумма позиции",
        ])
        for row in rows:
            writer.writerow([
                _csv_cell(row["order_number"]),
                _csv_cell(row["created_at"]),
                _csv_cell(ORDER_STATUSES.get(row["status"], row["status"])),
                _csv_cell(row["customer_name"]),
                _csv_cell(row["phone"]),
                _csv_cell(row["telegram"]),
                _csv_cell(DELIVERY_METHODS.get(row["delivery_method"], row["delivery_method"])),
                _csv_cell(row["address"]),
                _csv_cell(PAYMENT_METHODS.get(row["payment_method"], row["payment_method"])),
                f"{row['total_cents'] / 100:.2f}",
                "Да" if row["personal_data_consent"] else "Нет",
                _csv_cell(row["privacy_policy_version"]),
                _csv_cell(row["personal_data_consent_at"]),
                _csv_cell(row["barcode"]),
                _csv_cell(row["title"]),
                f"{row['unit_price_cents'] / 100:.2f}",
                row["quantity"],
                f"{row['subtotal_cents'] / 100:.2f}",
            ])
        data = ("\ufeff" + stream.getvalue()).encode("utf-8")
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=orders.csv"},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = str(exc.detail) if exc.detail else ("Страница не найдена" if exc.status_code == 404 else "Некорректный запрос")
        return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, message=message)

    return app
