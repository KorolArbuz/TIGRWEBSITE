from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_BUSY_TIMEOUT_MS = 3_000


class DatabaseBusyError(RuntimeError):
    """A short-lived SQLite lock that callers may safely retry."""


def is_database_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("database is locked", "database table is locked", "database is busy")
    )


@contextmanager
def translate_database_busy() -> Iterator[None]:
    try:
        yield
    except sqlite3.OperationalError as exc:
        if is_database_busy(exc):
            raise DatabaseBusyError("Database is temporarily busy") from exc
        raise

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NOT NULL UNIQUE,
    brand TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    box_qty TEXT NOT NULL DEFAULT '',
    source_price_cents INTEGER NOT NULL CHECK (source_price_cents >= 0),
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    description TEXT NOT NULL DEFAULT '',
    badge TEXT NOT NULL DEFAULT '',
    images_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    source_file TEXT NOT NULL DEFAULT '',
    source_sheet TEXT NOT NULL DEFAULT '',
    source_row INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    order_number TEXT UNIQUE,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    telegram TEXT NOT NULL DEFAULT '',
    delivery_method TEXT NOT NULL DEFAULT 'pickup',
    address TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT 'manager',
    comment TEXT NOT NULL DEFAULT '',
    personal_data_consent INTEGER NOT NULL DEFAULT 0 CHECK (personal_data_consent IN (0, 1)),
    privacy_policy_version TEXT NOT NULL DEFAULT '',
    personal_data_consent_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    barcode TEXT NOT NULL,
    title TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    subtotal_cents INTEGER NOT NULL CHECK (subtotal_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

-- Additive security state: existing orders and public links remain unchanged.
CREATE TABLE IF NOT EXISTS checkout_requests (
    scope_hash TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    PRIMARY KEY (scope_hash, key_hash)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent', 'failed', 'disabled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at REAL NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON notification_outbox(status, available_at);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    credential_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS security_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    count INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_expiry ON rate_limits(expires_at);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    deactivated INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    image_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def connect(path: str | Path, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = DEFAULT_BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60_000:
        raise ValueError("Invalid SQLite busy timeout")
    connection = sqlite3.connect(db_path, timeout=timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # SQLite does not accept a bound PRAGMA value; timeout_ms was type/range validated above.
    connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")  # nosec B608
    return connection


def init_db(path: str | Path) -> None:
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        # Lightweight forward-only migrations for databases created by earlier builds.
        # Serialize inspection and ALTER together when multiple workers start.
        connection.execute("BEGIN IMMEDIATE")
        order_columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()}
        migrations = {
            "personal_data_consent": "ALTER TABLE orders ADD COLUMN personal_data_consent INTEGER NOT NULL DEFAULT 0",
            "privacy_policy_version": "ALTER TABLE orders ADD COLUMN privacy_policy_version TEXT NOT NULL DEFAULT ''",
            "personal_data_consent_at": "ALTER TABLE orders ADD COLUMN personal_data_consent_at TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in order_columns:
                connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def get_setting(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, str(value)),
    )
