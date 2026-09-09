from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


def send_order_notification(token: str, chat_id: str, order: dict[str, Any], items: list[dict[str, Any]]) -> None:
    if not token or not chat_id:
        return
    lines = [
        f"🛒 Новый заказ {order['order_number']}",
        f"Клиент: {order['customer_name']}",
        f"Телефон: {order['phone']}",
    ]
    if order.get("telegram"):
        lines.append(f"Telegram: {order['telegram']}")
    lines.append("")
    for item in items[:30]:
        lines.append(f"• {item['title']} × {item['quantity']} — {item['subtotal_cents'] / 100:,.0f} ₽".replace(",", " "))
    if len(items) > 30:
        lines.append(f"…и ещё {len(items) - 30} позиций")
    lines.extend([
        "",
        f"Итого: {order['total_cents'] / 100:,.0f} ₽".replace(",", " "),
        f"Доставка: {order.get('delivery_method', '')}",
        f"Адрес: {order.get('address', '') or '—'}",
        f"Комментарий: {order.get('comment', '') or '—'}",
    ])
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 400:
            raise RuntimeError(f"Telegram API returned HTTP {response.status}")
        json.loads(response.read().decode("utf-8"))
