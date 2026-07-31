"""Replenishment order ledger with a human approval gate.

Every order an agent raises lands here as `pending_approval`. Nothing is
sent to StoreLink until a human approves it on the web approvals page.
The ledger is also what lets `get_stock_position` show open orders —
StoreLink has no list-orders endpoint, only get-by-id, so orders raised
outside this server are invisible to us (documented limitation).

In-memory for the pilot; swap for SQLite before real deployment so pending
approvals survive a restart.
"""

from __future__ import annotations

import datetime as dt
import itertools
import threading
from dataclasses import dataclass, field, asdict

from .storelink import StubStoreLinkClient

PENDING = "pending_approval"
SUBMITTED = "submitted"
REJECTED = "rejected"

OPEN_STATUSES = (PENDING, SUBMITTED)


@dataclass
class Order:
    order_id: str
    store_id: str
    sku: str
    sku_name: str
    quantity: int
    reason: str
    status: str = PENDING
    created_at: str = ""
    decided_at: str | None = None
    storelink_order_id: str | None = None
    expected_delivery: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


class OrderLedger:
    def __init__(self, client: StubStoreLinkClient):
        self._client = client
        self._orders: dict[str, Order] = {}
        self._seq = itertools.count(1001)
        self._lock = threading.Lock()

    def create(self, store_id: str, sku: str, sku_name: str, quantity: int, reason: str) -> Order:
        with self._lock:
            order = Order(
                order_id=f"RO-{next(self._seq)}",
                store_id=store_id,
                sku=sku,
                sku_name=sku_name,
                quantity=quantity,
                reason=reason,
                created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            )
            self._orders[order.order_id] = order
            return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def pending(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status == PENDING]

    def open_for(self, store_id: str, sku: str) -> list[Order]:
        return [o for o in self._orders.values()
                if o.store_id == store_id and o.sku == sku and o.status in OPEN_STATUSES]

    def approve(self, order_id: str) -> Order:
        """Human clicked Approve: submit to StoreLink, then mark submitted."""
        with self._lock:
            order = self._require_pending(order_id)
            result = self._client.create_replenishment(order.store_id, order.sku, order.quantity)
            order.status = SUBMITTED
            order.storelink_order_id = result["order_id"]
            order.expected_delivery = result["expected_delivery"]
            order.decided_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            return order

    def reject(self, order_id: str) -> Order:
        with self._lock:
            order = self._require_pending(order_id)
            order.status = REJECTED
            order.decided_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            return order

    def _require_pending(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order '{order_id}'.")
        if order.status != PENDING:
            raise ValueError(f"Order {order_id} is '{order.status}', not pending.")
        return order
