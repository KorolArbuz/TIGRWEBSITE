"""Bounded, thread-confined streaming export for private order history."""
from __future__ import annotations

import csv
import io
import queue
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import anyio

from .database import connect, translate_database_busy

CSV_FETCH_ROWS = 500
_END = object()


def csv_cell(value: Any) -> str:
    """Prevent spreadsheet formula execution when the CSV is opened."""
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


class _Failure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


class OrdersCsvExport:
    """Fetch and encode fixed-size batches on one dedicated DB thread."""

    def __init__(
        self,
        database_path: str | Path,
        statuses: Mapping[str, str],
        deliveries: Mapping[str, str],
        payments: Mapping[str, str],
        *,
        fetch_rows: int = CSV_FETCH_ROWS,
    ) -> None:
        self.database_path = database_path
        self.statuses, self.deliveries, self.payments = statuses, deliveries, payments
        self.fetch_rows = fetch_rows
        self._items: queue.Queue[bytes | _Failure | object] = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self.closed = threading.Event()
        self.max_batch_rows = 0
        self._thread = threading.Thread(target=self._produce, name="orders-csv-export", daemon=True)

    def _put(self, item: bytes | _Failure | object) -> bool:
        while not self._stop.is_set():
            try:
                self._items.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    @staticmethod
    def _encode(rows: list[list[Any]], *, bom: bool = False) -> bytes:
        output = io.StringIO(newline="")
        csv.writer(output, delimiter=";").writerows(rows)
        return (("\ufeff" if bom else "") + output.getvalue()).encode("utf-8")

    def _row(self, row: Any) -> list[Any]:
        return [
            csv_cell(row["order_number"]), csv_cell(row["created_at"]),
            csv_cell(self.statuses.get(row["status"], row["status"])), csv_cell(row["customer_name"]),
            csv_cell(row["phone"]), csv_cell(row["telegram"]),
            csv_cell(self.deliveries.get(row["delivery_method"], row["delivery_method"])),
            csv_cell(row["address"]), csv_cell(self.payments.get(row["payment_method"], row["payment_method"])),
            f"{row['total_cents'] / 100:.2f}", "Да" if row["personal_data_consent"] else "Нет",
            csv_cell(row["privacy_policy_version"]), csv_cell(row["personal_data_consent_at"]),
            csv_cell(row["barcode"]), csv_cell(row["title"]), f"{row['unit_price_cents'] / 100:.2f}",
            row["quantity"], f"{row['subtotal_cents'] / 100:.2f}",
        ]

    def _produce(self) -> None:
        try:
            with translate_database_busy(), closing(connect(self.database_path)) as db:
                with closing(db.execute(
                    """
                    SELECT o.order_number, o.created_at, o.status, o.customer_name, o.phone, o.telegram,
                           o.delivery_method, o.address, o.payment_method, o.total_cents,
                           o.personal_data_consent, o.privacy_policy_version, o.personal_data_consent_at,
                           i.barcode, i.title, i.unit_price_cents, i.quantity, i.subtotal_cents
                    FROM orders o JOIN order_items i ON i.order_id = o.id
                    ORDER BY o.id DESC, i.id
                    """
                )) as cursor:
                    header = [[
                        "Заказ", "Дата", "Статус", "Клиент", "Телефон", "Telegram", "Получение", "Адрес",
                        "Оплата", "Итого заказа", "Согласие ПД", "Версия политики", "Дата согласия",
                        "Штрихкод", "Товар", "Цена", "Количество", "Сумма позиции",
                    ]]
                    if not self._put(self._encode(header, bom=True)):
                        return
                    while not self._stop.is_set():
                        batch = cursor.fetchmany(self.fetch_rows)
                        self.max_batch_rows = max(self.max_batch_rows, len(batch))
                        if not batch:
                            break
                        if not self._put(self._encode([self._row(row) for row in batch])):
                            return
                    self._put(_END)
        except BaseException as exc:
            self._put(_Failure(exc))
        finally:
            # Cursor and connection contexts above close on this producer
            # thread, including cancellation and consumer disconnects.
            self.closed.set()

    async def start(self) -> bytes:
        self._thread.start()
        try:
            first = await anyio.to_thread.run_sync(self._items.get)
        except BaseException:
            await self.aclose()
            raise
        if isinstance(first, _Failure):
            await self.aclose()
            raise first.error
        if first is _END:
            await self.aclose()
            return b""
        return first

    async def body(self, first: bytes) -> AsyncIterator[bytes]:
        try:
            if first:
                yield first
            while True:
                item = await anyio.to_thread.run_sync(self._items.get)
                if item is _END:
                    break
                if isinstance(item, _Failure):
                    raise item.error
                yield item
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        self._stop.set()
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self._thread.join, 5)
