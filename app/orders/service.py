"""Business rules for orders.

Routes call these functions; these functions call ``repo``. Money is converted
to integer cents at the edge so totals never drift.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from app.db import transaction, utcnow_iso
from app.logging import get_logger
from app.orders import repo
from app.orders.models import OrderIn

log = get_logger(__name__)

ORDER_CREATED = "order.created"
ORDER_CANCELLED = "order.cancelled"


class OrderNotFound(Exception):
    def __init__(self, order_id: int) -> None:
        super().__init__(order_id)
        self.order_id = order_id


class InvalidTransition(Exception):
    pass


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used with a different request body."""


def to_cents(amount: float) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> float:
    return cents / 100


def order_total_cents(items: list[dict]) -> int:
    return sum(i["quantity"] * i["unit_price_cents"] for i in items)


def _request_hash(order: OrderIn) -> str:
    return hashlib.sha256(order.model_dump_json().encode()).hexdigest()


def create_order(
    conn: sqlite3.Connection,
    order: OrderIn,
    *,
    webhook_url: str = "",
    idempotency_key: str | None = None,
) -> dict:
    """Insert the order and its items, and queue an ``order.created`` webhook.

    With an ``idempotency_key``, a retry of the same request returns the order
    created the first time; the same key with a different body is a conflict.
    """
    if idempotency_key:
        row = conn.execute(
            "SELECT request_hash, order_id FROM idempotency_keys WHERE key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is not None:
            if row[0] != _request_hash(order):
                raise IdempotencyConflict(idempotency_key)
            return get_order(conn, row[1])
    items = [
        {"sku": i.sku, "quantity": i.quantity, "unit_price_cents": to_cents(i.unit_price)}
        for i in order.items
    ]
    total = order_total_cents(items)
    with transaction(conn):
        order_id = repo.insert_order(
            conn,
            customer_id=order.customer_id,
            currency=order.currency,
            total_cents=total,
            created_at=utcnow_iso(),
        )
        repo.insert_items(conn, order_id, items)
        if idempotency_key:
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash, order_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (idempotency_key, _request_hash(order), order_id, utcnow_iso()),
            )
        created = repo.get_order_with_items(conn, order_id)
        if webhook_url:
            _enqueue_webhook(conn, created, ORDER_CREATED, webhook_url)
    log.info("order %s created for %s: %s cents", order_id, order.customer_id, total)
    return created


def get_order(conn: sqlite3.Connection, order_id: int) -> dict:
    order = repo.get_order_with_items(conn, order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return order


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    return repo.list_orders_with_items(conn)


def cancel_order(
    conn: sqlite3.Connection, order_id: int, *, webhook_url: str = ""
) -> dict:
    with transaction(conn):
        order = get_order(conn, order_id)
        if order["status"] == "cancelled":
            return order
        if order["status"] != "pending":
            raise InvalidTransition(f"cannot cancel an order that is {order['status']}")
        repo.set_status(conn, order_id, "cancelled")
        order["status"] = "cancelled"
        if webhook_url:
            _enqueue_webhook(conn, order, ORDER_CANCELLED, webhook_url)
    log.info("order %s cancelled", order_id)
    return order


def _enqueue_webhook(conn: sqlite3.Connection, order: dict, event: str, url: str) -> None:
    payload = json.dumps({"event": event, "order": to_public(order)})
    conn.execute(
        "INSERT INTO webhook_deliveries (order_id, event, url, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (order["id"], event, url, payload, utcnow_iso()),
    )


def to_public(order: dict) -> dict:
    """Shape a DB row (cents) into the API response (major units)."""
    return {
        "id": order["id"],
        "customer_id": order["customer_id"],
        "status": order["status"],
        "currency": order["currency"],
        "total": from_cents(order["total_cents"]),
        "created_at": order["created_at"],
        "items": [
            {
                "sku": i["sku"],
                "quantity": i["quantity"],
                "unit_price": from_cents(i["unit_price_cents"]),
            }
            for i in order.get("items", [])
        ],
    }
