"""Security regressions use only a temporary database and synthetic customers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from shop.database import connect, init_db
from shop.orders import (
    CheckoutError,
    MAX_OUTBOX_ATTEMPTS,
    create_checkout,
    normalize_checkout,
    process_outbox_once,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    init_db(path)
    with closing(connect(path)) as db, db:
        db.execute(
            """INSERT INTO products(barcode, brand, model, source_price_cents, price_cents)
               VALUES ('synthetic-barcode', 'Example', 'Synthetic product', 10, 12345)"""
        )
    return path


def form(cart=None, **changes):
    values = {
        "customer_name": "Synthetic buyer",
        "phone": "0000000000",
        "personal_data_consent": "1",
        "cart_json": json.dumps(cart if cart is not None else [{"id": 1, "quantity": 2}]),
    }
    values.update(changes)
    return values


def checkout(database, *, scope="synthetic-scope-00000000000000", key="synthetic-key-0000000000000000", **changes):
    return create_checkout(database, scope, key, normalize_checkout(form(**changes)), "synthetic-policy-v1")


def counts(database):
    with closing(connect(database)) as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                     for name in ("orders", "order_items", "checkout_requests", "notification_outbox"))


@pytest.mark.parametrize("bad", [True, False, 1.0, 1.5, None, "1", -1, 0, 2**63, 10**100, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["id", "quantity"])
def test_cart_rejects_invalid_numbers_without_writes(database, bad, field):
    item = {"id": 1, "quantity": 2, field: bad}
    with pytest.raises(CheckoutError):
        checkout(database, cart=[item])
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("cart", [[], {}, [None], [{"id": 1}], [{"id": 1, "quantity": 1000}],
                                  [{"id": 1, "quantity": 1}] * 2,
                                  [{"id": number + 1, "quantity": 1} for number in range(51)]])
def test_invalid_cart_structure_is_not_partially_accepted(database, cart):
    with pytest.raises(CheckoutError):
        checkout(database, cart=cart)
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("raw", ["[", "[" * 1200, '[{"id":1,"id":2,"quantity":1}]', " " * 17000,
                                 '[{"id":1e100000,"quantity":1}]', '[{"id":1,"quantity":1,"x":NaN}]'])
def test_malformed_json_returns_validation_error(raw):
    with pytest.raises(CheckoutError):
        normalize_checkout(form(cart_json=raw))


@pytest.mark.parametrize("changes", [{"personal_data_consent": ""}, {"customer_name": "a" * 101},
                                    {"customer_name": "bad\x00text"}, {"phone": "1"},
                                    {"delivery_method": "elsewhere"}, {"payment_method": "unknown"},
                                    {"delivery_method": "courier", "address": ""}])
def test_checkout_requires_valid_fields_and_consent(database, changes):
    with pytest.raises(CheckoutError):
        checkout(database, **changes)
    assert counts(database) == (0, 0, 0, 0)


def test_concurrent_replay_creates_exactly_one_order_and_outbox(database):
    barrier = threading.Barrier(8)

    def submit():
        barrier.wait(timeout=10)
        return checkout(database)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: submit(), range(8)))
    assert len({result.order_id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    assert all(result.public_token == results[0].public_token for result in results)
    assert counts(database) == (1, 1, 1, 1)


def test_replay_conflict_is_scoped_and_new_key_is_allowed(database):
    first = checkout(database)
    with pytest.raises(CheckoutError) as error:
        checkout(database, comment="Different synthetic comment")
    assert error.value.status_code == 409
    different_guest = checkout(database, scope="synthetic-scope-other-00000000")
    intentional_repeat = checkout(database, key="synthetic-key-new-000000000000")
    assert len({first.order_id, different_guest.order_id, intentional_repeat.order_id}) == 3
    assert counts(database) == (3, 3, 3, 3)


def test_normalization_and_replay_preserve_original_price_and_consent(database):
    first = checkout(database, customer_name="  Synthetic buyer  ", cart=[{"id": 1, "quantity": 2, "price_cents": 1}])
    with closing(connect(database)) as db, db:
        db.execute("UPDATE products SET price_cents = 99999, active = 0")
    repeated = checkout(database)
    assert repeated.replayed
    assert first.order_id == repeated.order_id
    with closing(connect(database)) as db:
        row = db.execute("SELECT total_cents, personal_data_consent, privacy_policy_version, personal_data_consent_at FROM orders").fetchone()
        assert row["total_cents"] == 24690
        assert row["personal_data_consent"] == 1
        assert row["privacy_policy_version"] == "synthetic-policy-v1"
        assert row["personal_data_consent_at"]
        assert db.execute("SELECT unit_price_cents FROM order_items").fetchone()[0] == 12345


def test_unknown_product_and_excessive_total_create_no_order(database):
    with pytest.raises(CheckoutError):
        checkout(database, cart=[{"id": 1, "quantity": 1}, {"id": 2, "quantity": 1}])
    with closing(connect(database)) as db, db:
        db.execute("UPDATE products SET price_cents = ?", (10**10,))
    with pytest.raises(CheckoutError):
        checkout(database, cart=[{"id": 1, "quantity": 999}])
    assert counts(database) == (0, 0, 0, 0)


def test_outbox_insert_failure_rolls_back_entire_checkout(database):
    with closing(connect(database)) as db, db:
        db.execute("""CREATE TRIGGER synthetic_queue_failure BEFORE INSERT ON notification_outbox
                   BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        checkout(database)
    assert counts(database) == (0, 0, 0, 0)


def test_only_one_worker_claims_a_notification_without_holding_db_lock(database):
    checkout(database)
    entered = threading.Event()
    release = threading.Event()
    deliveries = []

    def slow_sender(*args):
        deliveries.append(1)
        entered.set()
        assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(process_outbox_once, database, "synthetic-token", "synthetic-chat", sender=slow_sender)
        try:
            assert entered.wait(timeout=5)
            assert not process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=slow_sender)
            # Checkout can still write while a notification is stalled.
            checkout(database, key="synthetic-key-second-0000000000")
        finally:
            release.set()
        assert first.result(timeout=5)
    assert len(deliveries) == 1
    with closing(connect(database)) as db:
        assert db.execute("SELECT status FROM notification_outbox ORDER BY id LIMIT 1").fetchone()[0] == "sent"


def test_retry_budget_leases_and_logs_do_not_expose_delivery_exception(database, caplog):
    checkout(database)
    marker = "synthetic-sensitive-error-marker"

    def failed_sender(*args):
        raise RuntimeError(marker)

    current = time.time() + 1
    for attempt in range(MAX_OUTBOX_ATTEMPTS):
        assert process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=failed_sender, now=current)
        assert not process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=failed_sender, now=current + 1)
        current += 4000
    assert not process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=failed_sender, now=current)
    with closing(connect(database)) as db:
        row = db.execute("SELECT status, attempts, lease_token FROM notification_outbox").fetchone()
        assert tuple(row) == ("failed", MAX_OUTBOX_ATTEMPTS, None)
    assert marker not in caplog.text


def test_abandoned_lease_can_be_recovered(database):
    checkout(database)
    with closing(connect(database)) as db, db:
        db.execute("UPDATE notification_outbox SET status = 'processing', lease_until = 1, lease_token = 'synthetic-lease', attempts = 1")
    assert process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=lambda *args: None)
    with closing(connect(database)) as db:
        assert tuple(db.execute("SELECT status, attempts FROM notification_outbox").fetchone()) == ("sent", 2)


def test_expired_worker_cannot_overwrite_new_worker_acknowledgement(database):
    checkout(database)
    current = time.time() + 1

    def stale_sender(*args):
        assert process_outbox_once(database, "synthetic-token", "synthetic-chat",
                                   sender=lambda *unused: None, now=current + 61)
        raise RuntimeError("Synthetic stale worker failure")

    assert process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=stale_sender, now=current)
    with closing(connect(database)) as db:
        assert tuple(db.execute("SELECT status, attempts FROM notification_outbox").fetchone()) == ("sent", 2)


def test_unconfigured_notifications_are_recorded_as_disabled(database):
    create_checkout(database, "synthetic-scope-00000000000000", "synthetic-key-0000000000000000",
                    normalize_checkout(form()), "synthetic-policy-v1", notify=False)
    assert not process_outbox_once(database, "synthetic-token", "synthetic-chat", sender=lambda *args: pytest.fail("Must not send"))
    with closing(connect(database)) as db:
        assert db.execute("SELECT status FROM notification_outbox").fetchone()[0] == "disabled"


@pytest.mark.parametrize("body", [b'{"ok": false}', b'[]', b"x" * (64 * 1024 + 1)],
                         ids=["rejected", "not-an-object", "oversized"])
def test_telegram_rejected_or_oversized_response_is_not_acknowledged(monkeypatch, body):
    from shop import telegram

    class SyntheticResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, maximum):
            return body[:maximum]

    monkeypatch.setattr(telegram.urllib.request, "urlopen", lambda request, timeout: SyntheticResponse())
    with pytest.raises(RuntimeError):
        telegram.send_order_notification("synthetic-token", "synthetic-chat", {
            "order_number": "SYNTHETIC", "customer_name": "Synthetic buyer", "phone": "0000000000", "total_cents": 100,
        }, [])


def test_additive_migration_preserves_catalog_order_items_and_consent(database):
    checkout(database)
    with closing(connect(database)) as db, db:
        # Simulate the previous application schema, which had no security tables.
        for table in ("checkout_requests", "notification_outbox", "admin_sessions", "security_state", "rate_limits"):
            db.execute(f"DROP TABLE {table}")
        before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                  for table in ("products", "orders", "order_items")}
    init_db(database)
    init_db(database)
    with closing(connect(database)) as db:
        # Compare with a boolean to avoid showing synthetic order tokens in failures.
        preserved = all([tuple(row) for row in db.execute(f"SELECT * FROM {table}")] == rows
                        for table, rows in before.items())
        assert preserved
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"checkout_requests", "notification_outbox", "admin_sessions", "security_state", "rate_limits"} <= tables


def test_concurrent_startup_migrates_legacy_consent_columns_once(database):
    checkout(database)
    with closing(connect(database)) as db, db:
        for column in ("personal_data_consent", "privacy_policy_version", "personal_data_consent_at"):
            db.execute(f"ALTER TABLE orders DROP COLUMN {column}")
        preserved_total = db.execute("SELECT total_cents FROM orders").fetchone()[0]
    barrier = threading.Barrier(2)

    def startup(_):
        barrier.wait(timeout=10)
        init_db(database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(startup, range(2)))
    with closing(connect(database)) as db:
        row = db.execute("SELECT total_cents,personal_data_consent,privacy_policy_version,personal_data_consent_at FROM orders").fetchone()
        assert tuple(row) == (preserved_total, 0, "", "")
        assert db.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
