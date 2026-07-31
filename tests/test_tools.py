"""Tests for the agent-facing tool surface and the approval lifecycle."""

import pytest

from src import server
from src.orders import OrderLedger, PENDING, REJECTED, SUBMITTED
from src.storelink import NotFound, StubStoreLinkClient

ST, SKU = "ST-014", "SKU-0451"  # crafted low-stock showcase: 12 on hand


# --- read tools -----------------------------------------------------------

def test_list_stores_shape():
    stores = server.list_stores()
    assert len(stores) == 8
    assert {"store_id", "name", "city", "region"} <= stores[0].keys()


def test_madeta_butter_scenario_differentiates_stores():
    """Demo prompt: order where last-24h sales minus on-hand exceeds 6 units."""
    def gap(store):
        pos = server.get_stock_position(store, "8847291")
        h = server.get_sales_history(store, "8847291", days=2)
        return sum(d["units_sold"] for d in h["days"]) - pos["on_hand"]

    assert gap("ST-047") > 6       # real shortfall -> should order
    assert gap("ST-102") <= 6      # claim is false here -> should not order
    assert server.get_stock_position("ST-047", "8847291")["stockout_risk"] is True
    assert server.get_stock_position("ST-102", "8847291")["stockout_risk"] is False


def test_stock_position_derives_cover_and_risk():
    pos = server.get_stock_position(ST, SKU)
    assert pos["on_hand"] == 12
    assert pos["avg_daily_units_7d"] > 0
    assert pos["days_of_cover"] == round(12 / pos["avg_daily_units_7d"], 1)
    assert pos["supplier_lead_time_days"] == 2
    assert pos["stockout_risk"] == (pos["days_of_cover"] < 2)
    assert pos["open_orders"] == []


def test_stock_position_zero_velocity_gives_null_cover(monkeypatch):
    monkeypatch.setattr(server, "_daily_sales",
                        lambda store, sku, days: [{"date": f"d{i}", "units_sold": 0} for i in range(days)])
    pos = server.get_stock_position(ST, SKU)
    assert pos["days_of_cover"] is None
    assert pos["stockout_risk"] is False


def test_sales_history_aggregates_days():
    hist = server.get_sales_history(ST, SKU, days=14)
    assert len(hist["days"]) == 14
    assert hist["total_units"] == sum(d["units_sold"] for d in hist["days"])
    with pytest.raises(ValueError):
        server.get_sales_history(ST, SKU, days=90)


def test_unknown_store_is_a_clear_error():
    with pytest.raises(NotFound):
        server.get_stock_position("ST-999", SKU)


# --- write path: human approval gate --------------------------------------

def _fresh_ledger():
    client = StubStoreLinkClient()
    return client, OrderLedger(client)


def test_order_requires_reason_and_positive_qty():
    with pytest.raises(ValueError):
        server.create_replenishment_order(ST, SKU, 0, "restock")
    with pytest.raises(ValueError):
        server.create_replenishment_order(ST, SKU, 100, "   ")


def test_order_lifecycle_approve():
    client, ledger = _fresh_ledger()
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 120, "12 on hand, 9/day, 2d lead")
    assert order.status == PENDING
    assert client._orders == {}  # nothing sent to StoreLink yet

    approved = ledger.approve(order.order_id)
    assert approved.status == SUBMITTED
    assert approved.storelink_order_id.startswith("SL-")
    assert approved.expected_delivery  # ETA comes back from StoreLink

    # once decided, it cannot be re-decided
    with pytest.raises(ValueError):
        ledger.reject(order.order_id)


def test_order_lifecycle_reject_never_reaches_storelink():
    client, ledger = _fresh_ledger()
    order = ledger.create(ST, SKU, "Fjord Smoked Salmon 200g", 120, "test")
    ledger.reject(order.order_id)
    assert ledger.get(order.order_id).status == REJECTED
    assert client._orders == {}  # StoreLink never saw it


def test_tool_flow_pending_then_visible_as_open_order():
    result = server.create_replenishment_order(ST, SKU, 120, "12 on hand, 9.5/day, 2d lead time")
    assert result["status"] == PENDING
    assert "approvals_page" in result

    status = server.get_replenishment_order(result["order_id"])
    assert status["status"] == PENDING

    pos = server.get_stock_position(ST, SKU)
    assert any(o["order_id"] == result["order_id"] for o in pos["open_orders"])

    # a second order for the same gap warns about the first
    dup = server.create_replenishment_order(ST, SKU, 60, "more")
    assert result["order_id"] in dup["warning"]
