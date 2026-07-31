"""StoreLink API client.

`StubStoreLinkClient` fakes the StoreLink API with deterministic in-memory
data so the MCP server is fully runnable without Korral network access.
A real client would implement the same methods over HTTPS, attaching the
per-store `X-Korral-Store-Key` header obtained from `StoreKeyProvider`.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass


class StoreLinkError(Exception):
    """Base error for StoreLink calls."""


class NotFound(StoreLinkError):
    """Unknown store / SKU / supplier / order."""


class StoreKeyProvider:
    """Resolves the per-store API key.

    Keys are scoped to a single store and rotated weekly by Korral IT, so
    they must never be baked into images or shown to the agent. The real
    implementation reads them from a mounted secrets file that Korral IT
    overwrites on rotation; the stub just fabricates one.
    """

    def get_key(self, store_id: str) -> str:
        return f"stub-key-{store_id}"


# --- deterministic mock dataset -------------------------------------------

_STORES = [
    {"store_id": "ST-001", "name": "Korral Amsterdam Centrum", "city": "Amsterdam", "region": "NL-West"},
    {"store_id": "ST-002", "name": "Korral Rotterdam Blaak", "city": "Rotterdam", "region": "NL-West"},
    {"store_id": "ST-014", "name": "Korral Antwerpen Meir", "city": "Antwerp", "region": "BE-North"},
    {"store_id": "ST-021", "name": "Korral Köln Ehrenfeld", "city": "Cologne", "region": "DE-West"},
    {"store_id": "ST-022", "name": "Korral München Schwabing", "city": "Munich", "region": "DE-South"},
    {"store_id": "ST-030", "name": "Korral København Vesterbro", "city": "Copenhagen", "region": "DK"},
]

_SUPPLIERS = {
    "SUP-01": {"supplier_id": "SUP-01", "name": "Nordkyst Seafood ApS", "lead_time_days": 2},
    "SUP-02": {"supplier_id": "SUP-02", "name": "Alpenmilch Molkerei GmbH", "lead_time_days": 3},
    "SUP-03": {"supplier_id": "SUP-03", "name": "Terra Iberica Imports SL", "lead_time_days": 7},
    "SUP-04": {"supplier_id": "SUP-04", "name": "Boulangerie Fournil SARL", "lead_time_days": 1},
}

_SKUS = {
    "SKU-0451": {"sku": "SKU-0451", "name": "Fjord Smoked Salmon 200g", "category": "seafood", "supplier_id": "SUP-01"},
    "SKU-0452": {"sku": "SKU-0452", "name": "Pickled Herring Jar 400g", "category": "seafood", "supplier_id": "SUP-01"},
    "SKU-1103": {"sku": "SKU-1103", "name": "Bergkäse Alt 48% 250g", "category": "dairy", "supplier_id": "SUP-02"},
    "SKU-1108": {"sku": "SKU-1108", "name": "Alpine Butter Unsalted 250g", "category": "dairy", "supplier_id": "SUP-02"},
    "SKU-2210": {"sku": "SKU-2210", "name": "Jamón Serrano Sliced 100g", "category": "charcuterie", "supplier_id": "SUP-03"},
    "SKU-2215": {"sku": "SKU-2215", "name": "Manchego Curado Wedge 200g", "category": "charcuterie", "supplier_id": "SUP-03"},
    "SKU-3001": {"sku": "SKU-3001", "name": "Sourdough Boule 600g", "category": "bakery", "supplier_id": "SUP-04"},
    "SKU-3004": {"sku": "SKU-3004", "name": "Rye Crispbread 350g", "category": "bakery", "supplier_id": "SUP-04"},
}

# Hand-crafted showcase positions: (store_id, sku) -> on_hand override.
# ST-014 salmon is the canonical "empty by afternoon" case.
_ON_HAND_OVERRIDES = {
    ("ST-014", "SKU-0451"): 12,
    ("ST-001", "SKU-3001"): 0,
    ("ST-021", "SKU-2210"): 400,
}


@dataclass
class StubReplenishmentOrder:
    order_id: str
    store_id: str
    sku: str
    quantity: int
    status: str
    expected_delivery: str


class StubStoreLinkClient:
    """Deterministic stand-in for the StoreLink HTTP API."""

    def __init__(self, keys: StoreKeyProvider | None = None, today: dt.date | None = None):
        self._keys = keys or StoreKeyProvider()
        self._today = today  # pin for tests; None = real today
        self._orders: dict[str, StubReplenishmentOrder] = {}
        self._order_seq = 7000

    def _now(self) -> dt.date:
        return self._today or dt.date.today()

    def _auth(self, store_id: str) -> None:
        # Real client: attach X-Korral-Store-Key header. Stub: validate store.
        if store_id not in {s["store_id"] for s in _STORES}:
            raise NotFound(f"Unknown store '{store_id}'. Use list_stores to see valid store ids.")
        self._keys.get_key(store_id)

    # --- reads ------------------------------------------------------------

    def list_stores(self) -> list[dict]:
        return [dict(s) for s in _STORES]

    def get_sku(self, sku: str) -> dict:
        if sku not in _SKUS:
            raise NotFound(f"Unknown SKU '{sku}'.")
        return dict(_SKUS[sku])

    def get_supplier(self, supplier_id: str) -> dict:
        if supplier_id not in _SUPPLIERS:
            raise NotFound(f"Unknown supplier '{supplier_id}'.")
        return dict(_SUPPLIERS[supplier_id])

    def get_inventory(self, store_id: str, sku: str) -> dict:
        self._auth(store_id)
        self.get_sku(sku)
        override = _ON_HAND_OVERRIDES.get((store_id, sku))
        if override is not None:
            on_hand = override
        else:
            rng = random.Random(f"inv:{store_id}:{sku}")
            on_hand = rng.randint(0, 12) * rng.randint(4, 25)
        return {"store_id": store_id, "sku": sku, "on_hand": on_hand,
                "last_updated": f"{self._now().isoformat()}T09:00:00Z"}

    def get_pos_transactions(self, store_id: str, sku: str, since: dt.date) -> list[dict]:
        """Individual POS transactions since `since` (what the real API returns)."""
        self._auth(store_id)
        self.get_sku(sku)
        rng = random.Random(f"pos:{store_id}:{sku}")
        base = rng.uniform(2.0, 30.0)  # this SKU's daily demand at this store
        txns = []
        day = since
        while day <= self._now():
            daily = max(0, int(rng.gauss(base, base * 0.3)))
            remaining = daily
            hour = 8
            while remaining > 0 and hour < 20:
                qty = min(remaining, rng.randint(1, 4))
                txns.append({"ts": f"{day.isoformat()}T{hour:02d}:{rng.randint(0, 59):02d}:00Z", "qty": qty})
                remaining -= qty
                hour += rng.randint(1, 3)
            day += dt.timedelta(days=1)
        return txns

    # --- writes -----------------------------------------------------------

    def create_replenishment(self, store_id: str, sku: str, quantity: int) -> dict:
        self._auth(store_id)
        sku_info = self.get_sku(sku)
        lead = _SUPPLIERS[sku_info["supplier_id"]]["lead_time_days"]
        self._order_seq += 1
        order = StubReplenishmentOrder(
            order_id=f"SL-{self._order_seq}",
            store_id=store_id,
            sku=sku,
            quantity=quantity,
            status="accepted",
            expected_delivery=(self._now() + dt.timedelta(days=lead)).isoformat(),
        )
        self._orders[order.order_id] = order
        return {"order_id": order.order_id, "status": order.status,
                "expected_delivery": order.expected_delivery}

    def get_replenishment(self, store_id: str, order_id: str) -> dict:
        self._auth(store_id)
        order = self._orders.get(order_id)
        if order is None or order.store_id != store_id:
            raise NotFound(f"No StoreLink order '{order_id}' for store {store_id}.")
        return {"order_id": order.order_id, "status": order.status,
                "quantity": order.quantity, "expected_delivery": order.expected_delivery}
