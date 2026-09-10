from __future__ import annotations

import secrets
from contextlib import closing

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from shop.app import create_app
from shop.database import connect


class SyntheticCredentials(tuple):
    def __repr__(self):
        return "<redacted synthetic credentials>"


class SyntheticConfig(dict):
    def __repr__(self):
        return "<redacted synthetic test configuration>"


@pytest.fixture(scope="session")
def admin_credentials():
    password = secrets.token_urlsafe(32)
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    return SyntheticCredentials((password, hasher.hash(password)))


@pytest.fixture
def app_config(tmp_path, admin_credentials):
    return SyntheticConfig({
        "APP_ENV": "test",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "ADMIN_PASSWORD": "",
        "ADMIN_PASSWORD_HASH": admin_credentials[1],
        "SESSION_COOKIE_SECURE": False,
        "ALLOWED_HOSTS": ["testserver", "localhost", "127.0.0.1"],
        "DATABASE_PATH": str(tmp_path / "synthetic-http.sqlite3"),
        "UPLOAD_ROOT": str(tmp_path / "media"),
        "IMPORT_ARCHIVE": str(tmp_path / "imports"),
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "STORE_NAME": "Synthetic store",
        "STORE_PHONE": "0000000000",
        "STORE_TELEGRAM": "",
        "LEGAL_OPERATOR_NAME": "Synthetic operator",
        "LEGAL_OPERATOR_ADDRESS": "Synthetic address",
        "LEGAL_OPERATOR_EMAIL": "synthetic@example.invalid",
        "PRIVACY_POLICY_VERSION": "synthetic-policy-v1",
        "LOGIN_RATE_LIMIT": 100,
        "LOGIN_RATE_WINDOW": 300,
        "CHECKOUT_RATE_LIMIT": 100,
        "CHECKOUT_RATE_WINDOW": 600,
    })


@pytest.fixture
def app(app_config):
    application = create_app(app_config)
    with closing(connect(app_config["DATABASE_PATH"])) as db, db:
        db.execute(
            """INSERT INTO products(barcode, brand, model, color, source_price_cents, price_cents,
                                    source_file, source_sheet, source_row)
               VALUES ('synthetic-http-barcode', 'Example', 'Synthetic product', '', 91237, 12345,
                       'private-synthetic-source.xlsx', 'private-synthetic-sheet', 71)"""
        )
    return application


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
