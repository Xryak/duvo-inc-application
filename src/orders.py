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

from . import audit
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
    decided_by: str | None = None  # self-asserted name from the approvals page
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
        # Audit outside the lock: the buyer's trail records the proposal, and a
        # slow disk should not serialise order creation.
        audit.record_proposed(order)
        return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def pending(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status == PENDING]

    def open_for(self, store_id: str, sku: str) -> list[Order]:
        return [o for o in self._orders.values()
                if o.store_id == store_id and o.sku == sku and o.status in OPEN_STATUSES]

    def approve(self, order_id: str, actor: dict | None = None) -> Order:
        """Human clicked Approve: submit to StoreLink, then mark submitted.

        `actor` names the person who approved (built by
        `audit.human_actor`); it is recorded on the order and in the audit
        trail. Two audit entries are written, not one — the approval and the
        StoreLink submission are distinct events, and if StoreLink refuses the
        order the buyer needs to see that their approval did *not* result in
        stock arriving.
        """
        actor = actor or audit.human_actor("")
        with self._lock:
            order = self._require_pending(order_id)
            try:
                result = self._client.create_replenishment(order.store_id, order.sku, order.quantity)
            except Exception as exc:
                # The human did approve; StoreLink is what refused. Both facts
                # go in the trail, and the order stays pending so it can be
                # retried once the upstream problem is fixed.
                audit.record_decision(order, approved=True, actor=actor)
                audit.record_submission_failed(order, actor=actor, error=exc)
                raise
            order.decided_by = actor.get("name")
            order.status = SUBMITTED
            order.storelink_order_id = result["order_id"]
            order.expected_delivery = result["expected_delivery"]
            order.decided_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        audit.record_decision(order, approved=True, actor=actor)
        audit.record_submitted(order, actor=actor)
        return order

    def reject(self, order_id: str, actor: dict | None = None) -> Order:
        actor = actor or audit.human_actor("")
        with self._lock:
            order = self._require_pending(order_id)
            order.status = REJECTED
            order.decided_by = actor.get("name")
            order.decided_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        audit.record_decision(order, approved=False, actor=actor)
        return order

    def _require_pending(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order '{order_id}'.")
        if order.status != PENDING:
            raise ValueError(f"Order {order_id} is '{order.status}', not pending.")
        return order
