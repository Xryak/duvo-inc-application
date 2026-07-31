"""StoreLink MCP server — the agent-facing tool surface.

Five tools, curated for a Korral category buyer's daily loop rather than
mirroring StoreLink's endpoints 1:1. Docstrings below double as the tool
descriptions the agent reads, so they state contracts, not implementation.
"""

from __future__ import annotations

import datetime as dt
import os

from mcp.server import MCPServer

from . import audit
from .diagnostics import ToolCallLogger, instrument_storelink
from .orders import OrderLedger
from .storelink import NotFound, StubStoreLinkClient, make_key_provider

# Instrumented at construction so every upstream StoreLink call is attributed
# to the tool call that caused it (see src/diagnostics.py).
client = instrument_storelink(StubStoreLinkClient(keys=make_key_provider()))
ledger = OrderLedger(client)

mcp = MCPServer(
    "storelink",
    instructions=(
        "Tools for a Korral category buyer's workflow: check stock positions, "
        "judge demand, and raise replenishment orders. Orders require human "
        "approval before they reach StoreLink."
    ),
)

# Logs every inbound MCP message with session/request correlation ids.
# Registered on the server rather than wrapped around each tool so a call that
# fails argument validation — never reaching a tool body — is still recorded.
mcp.middleware.append(ToolCallLogger())


def _approvals_url() -> str:
    host = os.getenv("APPROVALS_HOST", "127.0.0.1")
    port = os.getenv("APPROVALS_PORT", "8765")
    return f"http://{host}:{port}"


def _daily_sales(store_id: str, sku: str, days: int) -> list[dict]:
    since = dt.date.today() - dt.timedelta(days=days - 1)
    txns = client.get_pos_transactions(store_id, sku, since)
    totals: dict[str, int] = {}
    for t in txns:
        totals[t["ts"][:10]] = totals.get(t["ts"][:10], 0) + t["qty"]
    return [
        {"date": (since + dt.timedelta(days=i)).isoformat(),
         "units_sold": totals.get((since + dt.timedelta(days=i)).isoformat(), 0)}
        for i in range(days)
    ]


@mcp.tool()
def list_stores() -> list[dict]:
    """List all Korral stores (store_id, name, city, region, credentialed).

    Use the returned store_id in every other tool. `credentialed` says
    whether this server holds a StoreLink key for the store; calls for a
    store where it is false will fail until Korral IT provisions a key —
    report that to the user rather than retrying.
    """
    return client.list_stores()


@mcp.tool()
def get_stock_position(store_id: str, sku: str) -> dict:
    """Full stock picture for one SKU at one store, in a single call.

    Returns current on_hand, 7-day sales velocity, days_of_cover
    (on_hand / avg daily sales), the supplier's lead time, a computed
    stockout_risk flag (true when cover is shorter than lead time), and any
    open replenishment orders already raised through this server for this
    store+SKU. Check open_orders before raising a new order to avoid
    ordering twice for the same gap.

    days_of_cover is null when there were no sales in the last 7 days.
    """
    inventory = client.get_inventory(store_id, sku)
    sku_info = client.get_sku(sku)
    supplier = client.get_supplier(sku_info["supplier_id"])
    week = _daily_sales(store_id, sku, 7)

    avg_daily = round(sum(d["units_sold"] for d in week) / 7, 1)
    on_hand = inventory["on_hand"]
    days_of_cover = round(on_hand / avg_daily, 1) if avg_daily > 0 else None
    stockout_risk = days_of_cover is not None and days_of_cover < supplier["lead_time_days"]

    position = {
        "store_id": store_id,
        "sku": sku,
        "sku_name": sku_info["name"],
        "category": sku_info["category"],
        "on_hand": on_hand,
        "on_hand_last_updated": inventory["last_updated"],
        "avg_daily_units_7d": avg_daily,
        "units_sold_today": week[-1]["units_sold"],
        "days_of_cover": days_of_cover,
        "supplier_name": supplier["name"],
        "supplier_lead_time_days": supplier["lead_time_days"],
        "stockout_risk": stockout_risk,
        "open_orders": [o.to_dict() for o in ledger.open_for(store_id, sku)],
    }
    # Keep the numbers the agent just saw, so an order it raises next carries
    # the evidence it was based on into the buyer's audit trail.
    audit.note_position_seen(store_id, sku, position)
    return position


@mcp.tool()
def get_sales_history(store_id: str, sku: str, days: int = 14) -> dict:
    """Daily units sold for a SKU at a store over the last `days` days (1-60).

    Use this to judge whether current demand is a trend or a one-day spike
    before deciding an order quantity. Returns one row per day plus totals;
    individual POS transactions are not exposed.
    """
    if not 1 <= days <= 60:
        raise ValueError("days must be between 1 and 60.")
    daily = _daily_sales(store_id, sku, days)
    total = sum(d["units_sold"] for d in daily)
    return {
        "store_id": store_id,
        "sku": sku,
        "days": daily,
        "total_units": total,
        "avg_daily_units": round(total / days, 1),
    }


@mcp.tool()
def create_replenishment_order(store_id: str, sku: str, quantity: int, reason: str) -> dict:
    """Propose a replenishment order. A human must approve it before it is
    sent to StoreLink.

    The order is queued as status 'pending_approval' and shown to a Korral
    buyer on the approvals page; only on their approval is it submitted.
    `reason` is required and shown to the approver — state the evidence,
    e.g. "on-hand 12, selling 9.5/day, 3-day lead time". Poll
    get_replenishment_order to see whether it was approved (-> 'submitted',
    with delivery date) or 'rejected'.
    """
    if quantity <= 0:
        raise ValueError("quantity must be a positive number of units.")
    if not reason.strip():
        raise ValueError("reason is required — state the evidence for this order.")
    sku_info = client.get_sku(sku)
    client.get_inventory(store_id, sku)  # validates store + key scope

    existing = ledger.open_for(store_id, sku)
    order = ledger.create(store_id, sku, sku_info["name"], quantity, reason.strip())

    result = {
        "order_id": order.order_id,
        "status": order.status,
        "store_id": store_id,
        "sku": sku,
        "quantity": quantity,
        "approvals_page": _approvals_url(),
    }
    if existing:
        result["warning"] = (
            f"{len(existing)} open order(s) already exist for this store+SKU: "
            + ", ".join(f"{o.order_id} ({o.quantity}u, {o.status})" for o in existing)
        )
    return result


@mcp.tool()
def get_replenishment_order(order_id: str) -> dict:
    """Status of a replenishment order raised through this server.

    Statuses: 'pending_approval' (waiting for a human), 'submitted'
    (approved and sent to StoreLink; includes expected_delivery), or
    'rejected' (a human declined it — do not re-raise the same order
    without new evidence).
    """
    order = ledger.get(order_id)
    if order is None:
        raise NotFound(f"Unknown order '{order_id}'. Only orders created by this server can be looked up.")
    return order.to_dict()
