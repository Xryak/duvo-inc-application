"""StoreLink API client.

`StubStoreLinkClient` fakes the StoreLink API with deterministic in-memory
data so the MCP server is fully runnable without Korral network access.
A real client would implement the same methods over HTTPS, attaching the
per-store `X-Korral-Store-Key` header obtained from `StoreKeyProvider`.

Key handling is real, not stubbed: every store-scoped call is wrapped by
`_store_call`, which fetches the store's current key, and — if StoreLink
rejects it because Korral IT rotated keys while the request was in flight —
reloads the secrets and retries exactly once. A key that is still rejected
fails as `KeyExpired`; a store we hold no credential for fails up front as
`MissingStoreKey`. Neither error ever contains a key value.

Key events (secrets loaded, rotation retried, key expired) are emitted to the
diagnostic stream as well as stderr, carrying store ids and counts but never
key material — a transparent retry is invisible to the agent by design, and
"did this week's rotation cost us anything?" has to be answerable from
`python -m src.logquery`, not from a scrollback buffer.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
import random
import threading
from dataclasses import dataclass

from . import diagnostics

log = logging.getLogger("storelink")


class StoreLinkError(Exception):
    """Base error for StoreLink calls."""


class NotFound(StoreLinkError):
    """Unknown store / SKU / supplier / order."""


class MissingStoreKey(StoreLinkError):
    """This server holds no credential for an otherwise-valid store."""


class KeyExpired(StoreLinkError):
    """StoreLink rejected the store's key even after reloading secrets."""


def _missing_key_message(store_id: str) -> str:
    return (
        f"No StoreLink credential is configured for store '{store_id}'. "
        "This server can only act on stores it holds a key for — see the "
        "'credentialed' flag on list_stores. Other stores are unaffected; "
        f"ask Korral IT to provision a key for {store_id}."
    )


class StoreKeyProvider:
    """Resolves the per-store `X-Korral-Store-Key`.

    Keys are scoped to a single store and rotated weekly by Korral IT, so
    they must never be baked into images or shown to the agent. `get_key`
    returns the current key (raising `MissingStoreKey` when the store has
    none); `refresh` makes the next lookup see the latest secrets and is
    called by the client after StoreLink rejects a key mid-flight.
    """

    def get_key(self, store_id: str) -> str:
        raise NotImplementedError

    def refresh(self) -> None:
        pass

    def has_key(self, store_id: str) -> bool:
        try:
            self.get_key(store_id)
            return True
        except MissingStoreKey:
            return False

    def describe(self) -> str:
        return type(self).__name__


class StaticStoreKeyProvider(StoreKeyProvider):
    """Fixed in-memory keys — for development and tests only."""

    def __init__(self, keys: dict[str, str]):
        self._keys = dict(keys)

    def get_key(self, store_id: str) -> str:
        try:
            return self._keys[store_id]
        except KeyError:
            raise MissingStoreKey(_missing_key_message(store_id)) from None

    def describe(self) -> str:
        return f"static dev keys ({len(self._keys)} stores)"


class FileStoreKeyProvider(StoreKeyProvider):
    """Reads keys from a JSON file `{store_id: key}` mounted by Korral IT.

    IT overwrites the file on weekly rotation, so the file is stat'ed on
    every lookup and re-read when it changes — rotations are picked up
    without a restart. `refresh()` re-reads unconditionally, which covers
    a rotation that beats the mtime check (same-second overwrite, NFS
    attribute caching). A malformed overwrite (e.g. we read mid-write)
    keeps the last good keys rather than taking every store down; only at
    startup is an unreadable file fatal — better to refuse to start than
    to run with no credentials.
    """

    def __init__(self, path: str):
        self._path = path
        self._keys: dict[str, str] = {}
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()
        with self._lock:
            self._load(strict=True)

    def _load(self, strict: bool = False) -> None:
        try:
            stat = os.stat(self._path)
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and v for k, v in raw.items()
            ):
                raise ValueError("expected a JSON object mapping store_id to key")
        except (OSError, ValueError) as e:
            if strict:
                raise StoreLinkError(
                    f"Cannot load StoreLink secrets file '{self._path}': {e}"
                ) from e
            log.warning(
                "Secrets file %s unreadable (%s); keeping the %d previously loaded keys",
                self._path, e, len(self._keys),
            )
            diagnostics.note("storelink_keys_reload_failed", path=self._path, error=str(e),
                             keys_retained=len(self._keys))
            return
        changed = sorted(
            store for store in set(raw) | set(self._keys)
            if raw.get(store) != self._keys.get(store)
        )
        self._keys = raw
        self._stamp = (stat.st_mtime_ns, stat.st_size)
        # Store ids only — never key material. This is how an FDE proves a
        # rotation actually reached this server, and when.
        diagnostics.note("storelink_keys_loaded", path=self._path, stores=len(raw),
                         changed=changed or None)

    def _maybe_reload(self) -> None:
        try:
            stat = os.stat(self._path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._load()

    def get_key(self, store_id: str) -> str:
        with self._lock:
            self._maybe_reload()
            key = self._keys.get(store_id)
        if not key:
            raise MissingStoreKey(_missing_key_message(store_id))
        return key

    def refresh(self) -> None:
        with self._lock:
            self._load()

    def describe(self) -> str:
        return f"secrets file {self._path} ({len(self._keys)} stores)"


def dev_key_provider() -> StaticStoreKeyProvider:
    """Fabricated keys for every stub store — keeps the demo runnable with
    zero setup. Real deployments set STORELINK_KEYS_FILE instead."""
    return StaticStoreKeyProvider({s["store_id"]: f"dev-key-{s['store_id']}" for s in _STORES})


def make_key_provider() -> StoreKeyProvider:
    path = os.getenv("STORELINK_KEYS_FILE")
    return FileStoreKeyProvider(path) if path else dev_key_provider()


# --- deterministic mock dataset -------------------------------------------

_STORES = [
    {"store_id": "ST-001", "name": "Korral Amsterdam Centrum", "city": "Amsterdam", "region": "NL-West"},
    {"store_id": "ST-002", "name": "Korral Rotterdam Blaak", "city": "Rotterdam", "region": "NL-West"},
    {"store_id": "ST-014", "name": "Korral Antwerpen Meir", "city": "Antwerp", "region": "BE-North"},
    {"store_id": "ST-021", "name": "Korral Köln Ehrenfeld", "city": "Cologne", "region": "DE-West"},
    {"store_id": "ST-022", "name": "Korral München Schwabing", "city": "Munich", "region": "DE-South"},
    {"store_id": "ST-030", "name": "Korral København Vesterbro", "city": "Copenhagen", "region": "DK"},
    {"store_id": "ST-047", "name": "Korral Praha Vinohrady", "city": "Prague", "region": "CZ"},
    {"store_id": "ST-102", "name": "Korral Brno Střed", "city": "Brno", "region": "CZ"},
]

_SUPPLIERS = {
    "SUP-01": {"supplier_id": "SUP-01", "name": "Nordkyst Seafood ApS", "lead_time_days": 2},
    "SUP-02": {"supplier_id": "SUP-02", "name": "Alpenmilch Molkerei GmbH", "lead_time_days": 3},
    "SUP-03": {"supplier_id": "SUP-03", "name": "Terra Iberica Imports SL", "lead_time_days": 7},
    "SUP-04": {"supplier_id": "SUP-04", "name": "Boulangerie Fournil SARL", "lead_time_days": 1},
    "SUP-05": {"supplier_id": "SUP-05", "name": "Madeta a.s.", "lead_time_days": 2},
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
    # Legacy numeric id kept as-is from Korral's old catalog import.
    "8847291": {"sku": "8847291", "name": "Madeta Butter 250g", "category": "dairy", "supplier_id": "SUP-05"},
}

# Hand-crafted showcase positions: (store_id, sku) -> on_hand override.
# ST-014 salmon is the canonical "empty by afternoon" case. The Madeta
# butter pair is a judgment test: ST-047 has a real gap, ST-102 does not.
_ON_HAND_OVERRIDES = {
    ("ST-014", "SKU-0451"): 12,
    ("ST-001", "SKU-3001"): 0,
    ("ST-021", "SKU-2210"): 400,
    ("ST-047", "8847291"): 2,
    ("ST-102", "8847291"): 25,
}

# (store_id, sku) -> forced base daily demand, overriding the seeded random.
_VELOCITY_OVERRIDES = {
    ("ST-047", "8847291"): 20.0,
    ("ST-102", "8847291"): 8.0,
}


@dataclass
class StubReplenishmentOrder:
    order_id: str
    store_id: str
    sku: str
    quantity: int
    status: str
    expected_delivery: str


def _store_scoped(method):
    """Wrap a client method whose first argument is a store_id in the
    key-handling logic of `_store_call`."""

    @functools.wraps(method)
    def wrapper(self, store_id, *args, **kwargs):
        return self._store_call(store_id, lambda: method(self, store_id, *args, **kwargs))

    return wrapper


class StubStoreLinkClient:
    """Deterministic stand-in for the StoreLink HTTP API."""

    def __init__(self, keys: StoreKeyProvider | None = None, today: dt.date | None = None):
        self._keys = keys or dev_key_provider()
        self._today = today  # pin for tests; None = real today
        self._orders: dict[str, StubReplenishmentOrder] = {}
        self._order_seq = 7000
        # What "StoreLink's side" currently accepts per store. The first key
        # presented for a store is pinned as current; tests simulate Korral IT
        # rotating it with rotate_key().
        self._accepted_keys: dict[str, str] = {}

    def _now(self) -> dt.date:
        return self._today or dt.date.today()

    def key_source(self) -> str:
        return self._keys.describe()

    def rotate_key(self, store_id: str, new_key: str) -> None:
        """Simulate Korral IT rotating the key on the StoreLink side."""
        self._accepted_keys[store_id] = new_key

    def _key_accepted(self, store_id: str, key: str) -> bool:
        return key == self._accepted_keys.setdefault(store_id, key)

    def _store_call(self, store_id: str, request):
        """Key handling for every store-scoped call.

        Fetch the store's current key and make the call. If StoreLink
        rejects the key — Korral IT rotated it while this request was in
        flight — reload the secrets and retry exactly once with the fresh
        key; if that is still rejected, the mounted secrets are stale and
        the call fails as KeyExpired. The retry is safe for the write too:
        a call rejected for auth was never processed by StoreLink.
        """
        if store_id not in {s["store_id"] for s in _STORES}:
            raise NotFound(f"Unknown store '{store_id}'. Use list_stores to see valid store ids.")
        key = self._keys.get_key(store_id)
        if not self._key_accepted(store_id, key):
            log.warning("StoreLink rejected key for %s; reloading secrets and retrying once", store_id)
            # Also into the diagnostic stream, correlated to the tool call in
            # flight: a rotation that is retried transparently is invisible to
            # the agent, and stderr is not queryable. "Did Tuesday's rotation
            # cost us anything?" is a question for `logquery`, not a terminal.
            diagnostics.note("storelink_key_rotation_retry", store_id=store_id)
            self._keys.refresh()
            key = self._keys.get_key(store_id)
            if not self._key_accepted(store_id, key):
                diagnostics.note("storelink_key_expired", store_id=store_id,
                                 key_source=self._keys.describe())
                raise KeyExpired(
                    f"StoreLink rejected this server's key for store '{store_id}', even "
                    "after reloading secrets: the key has expired and this week's rotation "
                    "has not reached this server yet. The request was NOT processed. Retry "
                    "shortly; if it persists, ask Korral IT to push the rotated key for "
                    f"{store_id} to this server's secrets file."
                )
        return request()

    # --- reads ------------------------------------------------------------

    def list_stores(self) -> list[dict]:
        return [dict(s, credentialed=self._keys.has_key(s["store_id"])) for s in _STORES]

    def get_sku(self, sku: str) -> dict:
        if sku not in _SKUS:
            raise NotFound(f"Unknown SKU '{sku}'.")
        return dict(_SKUS[sku])

    def get_supplier(self, supplier_id: str) -> dict:
        if supplier_id not in _SUPPLIERS:
            raise NotFound(f"Unknown supplier '{supplier_id}'.")
        return dict(_SUPPLIERS[supplier_id])

    @_store_scoped
    def get_inventory(self, store_id: str, sku: str) -> dict:
        self.get_sku(sku)
        override = _ON_HAND_OVERRIDES.get((store_id, sku))
        if override is not None:
            on_hand = override
        else:
            rng = random.Random(f"inv:{store_id}:{sku}")
            on_hand = rng.randint(0, 12) * rng.randint(4, 25)
        return {"store_id": store_id, "sku": sku, "on_hand": on_hand,
                "last_updated": f"{self._now().isoformat()}T09:00:00Z"}

    @_store_scoped
    def get_pos_transactions(self, store_id: str, sku: str, since: dt.date) -> list[dict]:
        """Individual POS transactions since `since` (what the real API returns)."""
        self.get_sku(sku)
        base = _VELOCITY_OVERRIDES.get(
            (store_id, sku), random.Random(f"pos:{store_id}:{sku}").uniform(2.0, 30.0))
        txns = []
        day = since
        while day <= self._now():
            # Seed per calendar day so a given day's sales are identical
            # regardless of the query window.
            rng = random.Random(f"pos:{store_id}:{sku}:{day.isoformat()}")
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

    @_store_scoped
    def create_replenishment(self, store_id: str, sku: str, quantity: int) -> dict:
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

    @_store_scoped
    def get_replenishment(self, store_id: str, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None or order.store_id != store_id:
            raise NotFound(f"No StoreLink order '{order_id}' for store {store_id}.")
        return {"order_id": order.order_id, "status": order.status,
                "quantity": order.quantity, "expected_delivery": order.expected_delivery}
