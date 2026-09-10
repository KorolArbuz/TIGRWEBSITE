"""Atomic checkout and a bounded, durable, at-least-once notification queue.

All public service functions are synchronous. Call them in a bounded executor;
each connection is opened, used and closed on that executor's own thread.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import connect, translate_database_busy
from .telegram import send_order_notification

LOGGER = logging.getLogger(__name__)
MAX_PRODUCT_ID = 2**63 - 1
MAX_PRICE_CENTS = 10**10
MAX_TOTAL_CENTS = 10**12
MAX_CART_BYTES = 16 * 1024
MAX_LINES = 50
MAX_QUANTITY = 999
MAX_OUTBOX_ATTEMPTS = 8
OUTBOX_LEASE_SECONDS = 60
_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


class CheckoutError(ValueError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class CheckoutResult:
    order_id: int
    public_token: str
    replayed: bool


def _text(form: Mapping[str, Any], name: str, maximum: int, default: str = "") -> str:
    value = form.get(name, default)
    if not isinstance(value, str):
        raise CheckoutError("Некорректное поле формы")
    if len(value) > maximum:
        raise CheckoutError("Поле формы слишком длинное")
    value = unicodedata.normalize("NFC", value.strip())
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\r\n\t" for char in value):
        raise CheckoutError("Недопустимые символы в форме")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON value")


def normalize_checkout(form: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete cart, never silently coerce or discard bad items."""
    payload = {
        "customer_name": _text(form, "customer_name", 100),
        "phone": _text(form, "phone", 40),
        "telegram": _text(form, "telegram", 80),
        "delivery_method": _text(form, "delivery_method", 20, "pickup"),
        "payment_method": _text(form, "payment_method", 20, "manager"),
        "address": _text(form, "address", 500),
        "comment": _text(form, "comment", 1000),
    }
    if len(payload["customer_name"]) < 2:
        raise CheckoutError("Укажите имя")
    if not 7 <= len(re.sub(r"\D", "", payload["phone"])) <= 20:
        raise CheckoutError("Укажите корректный телефон")
    if payload["delivery_method"] not in {"pickup", "courier"}:
        raise CheckoutError("Некорректный способ получения")
    if payload["payment_method"] not in {"manager", "cash"}:
        raise CheckoutError("Некорректный способ оплаты")
    if payload["delivery_method"] == "courier" and len(payload["address"]) < 5:
        raise CheckoutError("Для доставки нужен адрес")
    if _text(form, "personal_data_consent", 10).lower() not in {"1", "true", "yes", "on"}:
        raise CheckoutError("Для оформления заказа необходимо согласие на обработку персональных данных")
    payload["personal_data_consent"] = True
    raw = form.get("cart_json", "")
    if not isinstance(raw, str) or len(raw) > MAX_CART_BYTES:
        raise CheckoutError("Некорректная корзина")
    try:
        if len(raw.encode("utf-8")) > MAX_CART_BYTES:
            raise ValueError("Cart exceeds byte budget")
        cart = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_json_object)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise CheckoutError("Некорректная корзина") from exc
    if not isinstance(cart, list) or not 1 <= len(cart) <= MAX_LINES:
        raise CheckoutError("В заказе должно быть от 1 до 50 разных товаров")
    normalized_cart: list[dict[str, int]] = []
    seen: set[int] = set()
    for item in cart:
        if not isinstance(item, dict):
            raise CheckoutError("Некорректная позиция корзины")
        product_id, quantity = item.get("id"), item.get("quantity")
        if type(product_id) is not int or not 1 <= product_id <= MAX_PRODUCT_ID:
            raise CheckoutError("Некорректный идентификатор товара")
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
            raise CheckoutError("Количество должно быть целым числом от 1 до 999")
        if product_id in seen:
            raise CheckoutError("Товар указан в корзине несколько раз")
        seen.add(product_id)
        normalized_cart.append({"id": product_id, "quantity": quantity})
    payload["cart"] = sorted(normalized_cart, key=lambda item: item["id"])
    return payload


def create_checkout(
    database_path: str | Path,
    scope: str,
    idempotency_key: str,
    payload: dict[str, Any],
    privacy_policy_version: str,
    *,
    notify: bool = True,
) -> CheckoutResult:
    """Commit order, item snapshot, request identity and outbox in one transaction.

The scope must come from an unpredictable signed guest session, never a form
field. A new key creates a new intentional order in the same guest session.
"""
    if not isinstance(scope, str) or not _KEY_PATTERN.fullmatch(scope):
        raise CheckoutError("Обновите страницу оформления заказа")
    if not isinstance(idempotency_key, str) or not _KEY_PATTERN.fullmatch(idempotency_key):
        raise CheckoutError("Обновите страницу оформления заказа")
    scope_hash = hashlib.sha256(scope.encode("ascii")).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    payload_hash = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    with translate_database_busy(), closing(connect(database_path)) as db, db:
        # Serialize the check-and-create across processes as well as threads.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """SELECT r.payload_hash, o.id, o.public_token FROM checkout_requests r
               JOIN orders o ON o.id = r.order_id WHERE r.scope_hash = ? AND r.key_hash = ?""",
            (scope_hash, key_hash),
        ).fetchone()
        if existing:
            if not hmac.compare_digest(existing["payload_hash"], payload_hash):
                raise CheckoutError("Этот ключ уже использован для другого заказа. Обновите страницу.", 409)
            return CheckoutResult(int(existing["id"]), str(existing["public_token"]), True)

        cart = payload["cart"]
        ids = [item["id"] for item in cart]
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            # Only generated '?' placeholders are interpolated; all IDs are bound.
            f"SELECT id, barcode, brand, model, color, price_cents FROM products WHERE active = 1 AND id IN ({placeholders})",  # nosec B608
            ids,
        ).fetchall()
        products = {int(row["id"]): row for row in rows}
        if len(products) != len(ids):
            raise CheckoutError("Один или несколько товаров стали недоступны. Обновите корзину.")
        items: list[tuple[Any, ...]] = []
        total = 0
        for item in cart:
            row = products[item["id"]]
            price = row["price_cents"]
            if type(price) is not int or not 0 <= price <= MAX_PRICE_CENTS:
                raise CheckoutError("Недопустимая цена товара. Обратитесь к менеджеру.")
            subtotal = price * item["quantity"]
            total += subtotal
            if total > MAX_TOTAL_CENTS:
                raise CheckoutError("Сумма заказа превышает допустимый предел")
            title = " ".join(str(row[field] or "").strip() for field in ("brand", "model", "color")).strip()
            items.append((row["id"], row["barcode"], title, price, item["quantity"], subtotal))
        public_token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        cursor = db.execute(
            """INSERT INTO orders (
                public_token, customer_name, phone, telegram, delivery_method,
                address, payment_method, comment, personal_data_consent,
                privacy_policy_version, personal_data_consent_at, status, total_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'new', ?)""",
            (public_token, payload["customer_name"], payload["phone"], payload["telegram"],
             payload["delivery_method"], payload["address"], payload["payment_method"],
             payload["comment"], privacy_policy_version, now.isoformat(), total),
        )
        order_id = int(cursor.lastrowid)
        order_number = f"XK-{now:%Y%m%d}-{order_id:05d}"
        db.execute("UPDATE orders SET order_number = ? WHERE id = ?", (order_number, order_id))
        db.executemany(
            """INSERT INTO order_items (
                order_id, product_id, barcode, title, unit_price_cents, quantity, subtotal_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(order_id, *item) for item in items],
        )
        db.execute(
            "INSERT INTO checkout_requests(scope_hash, key_hash, payload_hash, order_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (scope_hash, key_hash, payload_hash, order_id, now.timestamp()),
        )
        db.execute(
            "INSERT INTO notification_outbox(order_id, status, available_at, created_at) VALUES (?, ?, ?, ?)",
            (order_id, "pending" if notify else "disabled", now.timestamp(), now.timestamp()),
        )
        return CheckoutResult(order_id, public_token, False)


def process_outbox_once(
    database_path: str | Path,
    token: str,
    chat_id: str,
    *,
    sender: Callable[..., None] | None = None,
    now: float | None = None,
) -> bool:
    """Claim at most one job, send outside the DB transaction, then acknowledge.

Telegram has no idempotency support. A crash after successful delivery but
before acknowledgement may send a duplicate after the lease expires.
"""
    if not token or not chat_id:
        return False
    current = time.time() if now is None else now
    lease = secrets.token_hex(24)
    with closing(connect(database_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE notification_outbox SET status = 'failed', lease_until = NULL, lease_token = NULL
               WHERE attempts >= ? AND (status = 'pending' OR (status = 'processing' AND lease_until <= ?))""",
            (MAX_OUTBOX_ATTEMPTS, current),
        )
        job = db.execute(
            """SELECT id, order_id, attempts FROM notification_outbox
               WHERE attempts < ? AND ((status = 'pending' AND available_at <= ?)
                 OR (status = 'processing' AND lease_until <= ?)) ORDER BY id LIMIT 1""",
            (MAX_OUTBOX_ATTEMPTS, current, current),
        ).fetchone()
        if job is None:
            return False
        attempt = int(job["attempts"]) + 1
        db.execute(
            """UPDATE notification_outbox SET status = 'processing', attempts = ?, lease_until = ?, lease_token = ?
               WHERE id = ?""",
            (attempt, current + OUTBOX_LEASE_SECONDS, lease, job["id"]),
        )
        order = dict(db.execute(
            """SELECT order_number, customer_name, phone, telegram, total_cents,
                      delivery_method, address, comment FROM orders WHERE id = ?""",
            (job["order_id"],),
        ).fetchone())
        items = [dict(row) for row in db.execute(
            "SELECT title, quantity, subtotal_cents FROM order_items WHERE order_id = ? ORDER BY id",
            (job["order_id"],),
        )]
    order["delivery_method"] = {"pickup": "Самовывоз", "courier": "Доставка"}.get(order["delivery_method"], "")
    try:
        (sender or send_order_notification)(token, chat_id, order, items)
    except Exception:
        # This boundary handles external delivery failures only. Do not log the
        # exception: HTTP errors can contain the bot token and recipient data.
        LOGGER.warning("Order notification delivery failed")
        status = "failed" if attempt >= MAX_OUTBOX_ATTEMPTS else "pending"
        delay = min(3600, 30 * (2 ** (attempt - 1)))
        with closing(connect(database_path)) as db, db:
            db.execute(
                """UPDATE notification_outbox SET status = ?, available_at = ?, lease_until = NULL, lease_token = NULL
                   WHERE id = ? AND lease_token = ? AND status = 'processing'""",
                (status, current + delay, job["id"], lease),
            )
    else:
        with closing(connect(database_path)) as db, db:
            db.execute(
                """UPDATE notification_outbox SET status = 'sent', sent_at = ?, lease_until = NULL, lease_token = NULL
                   WHERE id = ? AND lease_token = ? AND status = 'processing'""",
                (time.time(), job["id"], lease),
            )
    return True


class OutboxWorker:
    """One delivery thread per application process, sharing SQLite claim leases."""

    def __init__(self, database_path: str | Path, token: str, chat_id: str):
        self.database_path = database_path
        self.token = token
        self.chat_id = chat_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.token or not self.chat_id or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="notification-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = process_outbox_once(self.database_path, self.token, self.chat_id)
            except sqlite3.Error:
                LOGGER.warning("Notification queue temporarily unavailable")
                processed = False
            # No unbounded futures or per-order threads. Yield even with a full queue.
            self._stop.wait(0.05 if processed else 1.0)
