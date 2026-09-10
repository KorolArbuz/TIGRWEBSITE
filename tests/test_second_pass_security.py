from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import closing
from pathlib import Path

from detect_secrets.settings import default_settings

from scripts.scan_secrets import candidates
from shop.csv_export import OrdersCsvExport
from shop.database import connect, init_db


def populated_orders(tmp_path, count: int = 1203):
    database = tmp_path / "orders.sqlite3"
    init_db(database)
    with closing(connect(database)) as db, db:
        for number in range(count):
            cursor = db.execute(
                "INSERT INTO orders(public_token,order_number,customer_name,phone,total_cents,"
                "personal_data_consent,privacy_policy_version,personal_data_consent_at) "
                "VALUES (?,?,?,?,?,1,'synthetic-v1','2026-01-01T00:00:00Z')",
                (f"token-{number}", f"ORDER-{number}", "=SYNTHETIC()" if number == 0 else "Buyer",
                 "0000000000", 1234),
            )
            db.execute(
                "INSERT INTO order_items(order_id,barcode,title,unit_price_cents,quantity,subtotal_cents) "
                "VALUES (?,?,?,?,?,?)",
                (cursor.lastrowid, f"barcode-{number}", "Synthetic product", 1234, 1, 1234),
            )
    return database


def test_large_csv_export_is_chunked_bounded_and_preserves_history(tmp_path):
    database = populated_orders(tmp_path)
    export = OrdersCsvExport(database, {"new": "Новый"}, {"pickup": "Самовывоз"},
                             {"manager": "После подтверждения"}, fetch_rows=37)

    async def consume():
        first = await export.start()
        chunks = []
        async for chunk in export.body(first):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(consume())
    assert len(chunks) > 20
    assert export.max_batch_rows <= 37
    assert chunks[0].startswith(b"\xef\xbb\xbf")
    assert all(not chunk.startswith(b"\xef\xbb\xbf") for chunk in chunks[1:])
    rows = list(csv.reader(io.StringIO(b"".join(chunks).decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1204
    assert any(row[3] == "'=SYNTHETIC()" for row in rows[1:])
    assert export.closed.is_set()
    with closing(connect(database)) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1203


def test_csv_consumer_disconnect_closes_producer_connection(tmp_path):
    database = populated_orders(tmp_path, count=200)
    export = OrdersCsvExport(database, {}, {}, {}, fetch_rows=1)

    async def disconnect():
        first = await export.start()
        body = export.body(first)
        assert await anext(body)
        await body.aclose()

    asyncio.run(disconnect())
    assert export.closed.wait(timeout=1)


def test_new_synthetic_secret_fixture_is_not_silently_allowlisted():
    synthetic = "AKIA" + "SYNTHETICCONTROL"
    with default_settings():
        findings = list(candidates("synthetic-control.env", ("AWS_ACCESS_KEY_ID=" + synthetic).encode()))
    allowlist = json.loads((Path(__file__).parents[1] / ".secrets-allowlist.json").read_text(encoding="utf-8"))
    assert [finding["type"] for finding in findings] == ["AWS Access Key"]
    assert findings[0]["fingerprint"] not in allowlist
